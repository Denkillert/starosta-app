from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import (
    AttendanceStatus,
    DocStatus,
    DocType,
    MemberStatus,
    OccurrenceStatus,
)
from ..models import Attendance, Lesson, LessonOccurrence, Membership, Subject, User
from ..utils import lesson_fits_member, name_sort_key, now_local, subgroup_number
from . import audit
from .schedule import list_day, list_range
from .schedule_sync import latest_snapshot

VISIBLE = (
    AttendanceStatus.PRESENT,
    AttendanceStatus.ABSENT,
    AttendanceStatus.EXCUSED,
)

HOURS_PER_LESSON = max(1, settings.academic_hours_per_lesson)

STATUS_SYMBOL = {
    AttendanceStatus.PRESENT: "✓",
    AttendanceStatus.ABSENT: "Н",
    AttendanceStatus.EXCUSED: "У",
}

def effective_status(row: Attendance | None) -> str:
    if row is None:
        return AttendanceStatus.PRESENT
    if row.status in (AttendanceStatus.LATE, AttendanceStatus.NOT_ANSWERED):
        return AttendanceStatus.PRESENT
    return row.status

@dataclass(slots=True)
class OccurrenceView:
    occurrence: LessonOccurrence
    marks: dict[int, Attendance] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def lesson(self) -> Lesson:
        return self.occurrence.lesson

    @property
    def absent(self) -> int:
        return self.counts.get(AttendanceStatus.ABSENT, 0)

    @property
    def excused(self) -> int:
        return self.counts.get(AttendanceStatus.EXCUSED, 0)

    @property
    def marked(self) -> int:
        return self.absent + self.excused

    @property
    def present(self) -> int:
        return max(0, self.total - self.marked)

    @property
    def total(self) -> int:
        return len(self.marks)

    def status_for(self, member_id: int) -> str:
        return effective_status(self.marks.get(member_id))

    def applies(self, member: Membership) -> bool:
        """Относится ли пара к подгруппе студента."""
        return lesson_fits_member(self.lesson.subgroup, member.subgroup)

    def symbol_for(self, member_id: int) -> str:
        return STATUS_SYMBOL.get(self.status_for(member_id), "")

@dataclass(slots=True)
class StudentStats:
    member: Membership
    present: int = 0
    late: int = 0
    absent: int = 0
    excused: int = 0
    not_answered: int = 0
    total: int = 0

    @property
    def attended(self) -> int:
        return self.present + self.late

    @property
    def skipped(self) -> int:
        return self.absent

    @property
    def percent(self) -> int | None:
        base = self.attended + self.absent
        if not base:
            return None
        return round(self.attended * 100 / base)

@dataclass(slots=True)
class MonthStudent:
    member: Membership
    days: dict[int, int] = field(default_factory=dict)
    total_hours: int = 0
    excused_hours: int = 0
    absent_hours: int = 0

@dataclass(slots=True)
class PaidEntry:
    member: Membership
    day: date
    subject: str
    teacher: str
    kind: str
    hours: int

@dataclass(slots=True)
class MonthData:
    year: int
    month: int
    students: list[MonthStudent] = field(default_factory=list)
    paid_entries: list[PaidEntry] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        return {
            "total": sum(s.total_hours for s in self.students),
            "excused": sum(s.excused_hours for s in self.students),
            "absent": sum(s.absent_hours for s in self.students),
            "students": len(self.students),
        }

def _name_key(member: Membership) -> tuple[str, int]:
    """Ключ строго алфавитной сортировки, без учёта должности."""
    return name_sort_key(member.full_name), member.id


def roster(db: Session, group_id: int) -> list[Membership]:
    members = list(
        db.scalars(
            select(Membership).where(
                Membership.group_id == group_id,
                Membership.status == MemberStatus.ACTIVE,
                # Заявки по коду приглашения не считаются студентами, пока
                # староста не проверит ФИО и подгруппу.
                Membership.needs_review.is_(False),
            )
        ).all()
    )
    return sorted(members, key=_name_key)

def day_overview(
    db: Session, group_id: int, day: date, terms: int | None = None
) -> list[OccurrenceView]:
    members = roster(db, group_id)
    member_ids = [m.id for m in members]
    views: list[OccurrenceView] = []
    for occurrence in list_day(db, group_id, day):
        if occurrence.status == OccurrenceStatus.CANCELLED:
            continue
        if terms is not None and occurrence.lesson.term != terms:
            continue
        view = OccurrenceView(occurrence=occurrence)
        applicable = [
            member
            for member in members
            if lesson_fits_member(occurrence.lesson.subgroup, member.subgroup)
        ]
        applicable_ids = {member.id for member in applicable}
        for member in applicable:
            view.marks[member.id] = Attendance(
                occurrence_id=occurrence.id,
                member_id=member.id,
                status=AttendanceStatus.PRESENT,
            )
        if member_ids:
            rows = db.scalars(
                select(Attendance).where(
                    Attendance.occurrence_id == occurrence.id,
                    Attendance.member_id.in_(member_ids),
                )
            ).all()
            for row in rows:
                # Старые отметки «чужой» подгруппы не должны попадать в счёт.
                if row.member_id in applicable_ids:
                    view.marks[row.member_id] = row
        counts = {status: 0 for status in VISIBLE}
        for row in view.marks.values():
            status = effective_status(row)
            counts[status] = counts.get(status, 0) + 1
        view.counts = counts
        views.append(view)
    return views

def mark(
    db: Session,
    *,
    occurrence: LessonOccurrence,
    member: Membership,
    status: str,
    actor: User | None = None,
    comment: str | None = None,
    source: str = "head",
    document_id: int | None = None,
    commit: bool = True,
) -> Attendance:
    if status not in AttendanceStatus.ALL:
        raise ValueError(f"Неизвестный статус посещаемости: {status}")
    if occurrence.group_id != member.group_id:
        raise ValueError("Студент и пара относятся к разным группам")
    if occurrence.status == OccurrenceStatus.CANCELLED:
        raise ValueError("Нельзя отметить отменённую пару")
    if not lesson_fits_member(occurrence.lesson.subgroup, member.subgroup):
        raise ValueError(
            "Студент не в той подгруппе: эта пара для "
            f"«{occurrence.lesson.subgroup}»"
        )

    row = db.scalar(
        select(Attendance).where(
            Attendance.occurrence_id == occurrence.id,
            Attendance.member_id == member.id,
        )
    )
    if row is None:
        row = Attendance(occurrence_id=occurrence.id, member_id=member.id)
        db.add(row)

    if status in (AttendanceStatus.PRESENT, AttendanceStatus.ABSENT):
        row.document_id = None
        row.previous_status = None
    elif document_id is not None:
        row.document_id = document_id

    row.status = status
    row.comment = comment
    row.source = source
    row.marked_by_id = actor.id if actor else None
    row.marked_at = now_local()

    if commit:
        audit.log(
            db,
            action="attendance.mark",
            group_id=occurrence.group_id,
            actor_id=actor.id if actor else None,
            entity_type="occurrence",
            entity_id=occurrence.id,
            payload={"member_id": member.id, "status": status},
        )
        db.commit()
    return row

def toggle(
    db: Session,
    *,
    occurrence: LessonOccurrence,
    member: Membership,
    actor: User | None = None,
) -> str:
    row = db.scalar(
        select(Attendance).where(
            Attendance.occurrence_id == occurrence.id,
            Attendance.member_id == member.id,
        )
    )
    current = effective_status(row)
    target = (
        AttendanceStatus.PRESENT
        if current == AttendanceStatus.ABSENT
        else AttendanceStatus.ABSENT
    )
    mark(
        db,
        occurrence=occurrence,
        member=member,
        status=target,
        actor=actor,
        commit=True,
    )
    return target

def apply_changes(
    db: Session,
    *,
    group_id: int,
    changes: list[dict],
    actor: User | None = None,
) -> tuple[int, list[dict]]:
    """Сохраняет пачку отметок одной транзакцией.

    Сначала проверяются все строки. Если хоть одна некорректна, не
    сохраняется ничего — интерфейс не показывает «полуприменённые»
    отметки и может спокойно повторить запрос.
    """
    prepared: list[tuple[LessonOccurrence, Membership, str]] = []
    errors: list[dict] = []
    seen: set[tuple[int, int]] = set()

    for raw in changes:
        if not isinstance(raw, dict):
            errors.append({"reason": "Некорректная строка отметки"})
            continue
        try:
            occurrence_id = int(raw.get("occurrence_id") or 0)
            member_id = int(raw.get("member_id") or 0)
        except (TypeError, ValueError):
            errors.append({"reason": "Некорректная пара или студент"})
            continue

        key = (occurrence_id, member_id)
        if key in seen:
            continue
        seen.add(key)

        status = str(raw.get("status") or "")
        if status not in (AttendanceStatus.PRESENT, AttendanceStatus.ABSENT):
            errors.append(
                {
                    "occurrence_id": occurrence_id,
                    "member_id": member_id,
                    "reason": "Вручную можно ставить только «присутствует» или «пропуск»",
                }
            )
            continue

        occurrence = db.get(LessonOccurrence, occurrence_id)
        if occurrence is None or occurrence.group_id != group_id:
            errors.append(
                {
                    "occurrence_id": occurrence_id,
                    "member_id": member_id,
                    "reason": "Пара не найдена в этой группе",
                }
            )
            continue
        if occurrence.status == OccurrenceStatus.CANCELLED:
            errors.append(
                {
                    "occurrence_id": occurrence_id,
                    "member_id": member_id,
                    "reason": "Пара отменена",
                }
            )
            continue

        member = db.get(Membership, member_id)
        if (
            member is None
            or member.group_id != group_id
            or member.status != MemberStatus.ACTIVE
            or member.needs_review
        ):
            errors.append(
                {
                    "occurrence_id": occurrence_id,
                    "member_id": member_id,
                    "reason": "Студент не найден в этой группе",
                }
            )
            continue
        if not lesson_fits_member(occurrence.lesson.subgroup, member.subgroup):
            errors.append(
                {
                    "occurrence_id": occurrence_id,
                    "member_id": member_id,
                    "reason": "Пара относится к другой подгруппе",
                }
            )
            continue

        row = db.scalar(
            select(Attendance).where(
                Attendance.occurrence_id == occurrence_id,
                Attendance.member_id == member_id,
            )
        )
        if row is not None and effective_status(row) == AttendanceStatus.EXCUSED:
            errors.append(
                {
                    "occurrence_id": occurrence_id,
                    "member_id": member_id,
                    "reason": "«У» ставится документом и не меняется вручную",
                }
            )
            continue

        prepared.append((occurrence, member, status))

    if errors:
        return 0, errors

    for occurrence, member, status in prepared:
        mark(
            db,
            occurrence=occurrence,
            member=member,
            status=status,
            actor=actor,
            commit=False,
        )

    if prepared:
        audit.log(
            db,
            action="attendance.batch",
            group_id=group_id,
            actor_id=actor.id if actor else None,
            entity_type="group",
            entity_id=group_id,
            payload={"count": len(prepared)},
        )
        db.commit()
    return len(prepared), []


def reset_day(
    db: Session, group_id: int, day: date, actor: User | None = None
) -> int:
    occurrence_ids = [
        o.id
        for o in list_day(db, group_id, day)
        if o.status != OccurrenceStatus.CANCELLED
    ]
    if not occurrence_ids:
        return 0

    rows = db.scalars(
        select(Attendance).where(
            Attendance.occurrence_id.in_(occurrence_ids),
            Attendance.document_id.is_(None),
        )
    ).all()
    for row in rows:
        db.delete(row)
    audit.log(
        db,
        action="attendance.reset_day",
        group_id=group_id,
        actor_id=actor.id if actor else None,
        entity_type="group",
        entity_id=group_id,
        payload={"date": day.isoformat(), "count": len(rows)},
    )
    db.commit()
    return len(rows)

def summary(
    db: Session, group_id: int, date_from: date, date_to: date
) -> list[StudentStats]:
    members = roster(db, group_id)
    stats = {m.id: StudentStats(member=m) for m in members}
    if not stats:
        return []

    total_occurrences = db.execute(
        select(Lesson.subgroup, func.count(LessonOccurrence.id))
        .select_from(LessonOccurrence)
        .join(Lesson, LessonOccurrence.lesson_id == Lesson.id)
        .where(
            LessonOccurrence.group_id == group_id,
            LessonOccurrence.date >= date_from,
            LessonOccurrence.date <= date_to,
            LessonOccurrence.status == OccurrenceStatus.SCHEDULED,
        )
        .group_by(Lesson.subgroup)
    ).all()
    counts_by_subgroup: dict[int | None, int] = {
        subgroup_number(subgroup): int(count) for subgroup, count in total_occurrences
    }
    common_lessons = counts_by_subgroup.get(None, 0)

    rows = db.execute(
        select(
            Attendance.member_id,
            Attendance.status,
            Lesson.subgroup,
            func.count(Attendance.id),
        )
        .select_from(Attendance)
        .join(LessonOccurrence, Attendance.occurrence_id == LessonOccurrence.id)
        .join(Lesson, LessonOccurrence.lesson_id == Lesson.id)
        .where(
            LessonOccurrence.group_id == group_id,
            LessonOccurrence.date >= date_from,
            LessonOccurrence.date <= date_to,
            LessonOccurrence.status == OccurrenceStatus.SCHEDULED,
            Attendance.member_id.in_(list(stats.keys())),
        )
        .group_by(Attendance.member_id, Attendance.status, Lesson.subgroup)
    ).all()

    raw: dict[int, dict[str, int]] = {}
    for member_id, status, subgroup, count in rows:
        member = stats[member_id].member
        if not lesson_fits_member(subgroup, member.subgroup):
            continue
        bucket = raw.setdefault(member_id, {})
        bucket[status] = bucket.get(status, 0) + int(count)

    for member_id, item in stats.items():
        member = item.member
        if member.subgroup is None:
            total = sum(counts_by_subgroup.values())
        else:
            total = common_lessons + counts_by_subgroup.get(
                subgroup_number(member.subgroup), 0
            )
        counts = raw.get(member_id, {})
        item.absent = counts.get(AttendanceStatus.ABSENT, 0)
        item.excused = counts.get(AttendanceStatus.EXCUSED, 0)
        item.total = total
        item.present = max(0, total - item.absent - item.excused)

    return list(stats.values())

def totals(stats: list[StudentStats]) -> dict[str, int]:
    result = {
        "present": 0,
        "late": 0,
        "absent": 0,
        "excused": 0,
        "not_answered": 0,
        "total": 0,
        "attended": 0,
    }
    for item in stats:
        result["present"] += item.present
        result["late"] += item.late
        result["absent"] += item.absent
        result["excused"] += item.excused
        result["not_answered"] += item.not_answered
        result["total"] += item.total
        result["attended"] += item.attended
    base = result["attended"] + result["absent"]
    result["percent"] = round(result["attended"] * 100 / base) if base else 0
    return result

def subject_summary(
    db: Session, group_id: int, date_from: date, date_to: date
) -> list[dict]:
    members = roster(db, group_id)
    if not members:
        return []

    # Сколько студентов реально ходит на пару с такой подгруппой.
    members_by_subgroup: dict[int | None, int] = {}
    for member in members:
        key = subgroup_number(member.subgroup)
        members_by_subgroup[key] = members_by_subgroup.get(key, 0) + 1
    members_without_subgroup = members_by_subgroup.get(None, 0)

    def applicable_members(lesson_subgroup) -> int:
        subgroup = subgroup_number(lesson_subgroup)
        if subgroup is None:
            return len(members)
        return members_without_subgroup + members_by_subgroup.get(subgroup, 0)

    occurrences = db.execute(
        select(
            Lesson.subject_id,
            Subject.name,
            Lesson.subgroup,
            func.count(LessonOccurrence.id),
        )
        .select_from(LessonOccurrence)
        .join(Lesson, LessonOccurrence.lesson_id == Lesson.id)
        .join(Subject, Lesson.subject_id == Subject.id)
        .where(
            LessonOccurrence.group_id == group_id,
            LessonOccurrence.date >= date_from,
            LessonOccurrence.date <= date_to,
            LessonOccurrence.status == OccurrenceStatus.SCHEDULED,
        )
        .group_by(Lesson.subject_id, Subject.name, Lesson.subgroup)
    ).all()

    rows = db.execute(
        select(
            Lesson.subject_id,
            Subject.name,
            Lesson.subgroup,
            Membership.subgroup,
            func.sum(
                case((Attendance.status == AttendanceStatus.ABSENT, 1), else_=0)
            ).label("absent"),
            func.sum(
                case((Attendance.status == AttendanceStatus.EXCUSED, 1), else_=0)
            ).label("excused"),
        )
        .select_from(Attendance)
        .join(LessonOccurrence, Attendance.occurrence_id == LessonOccurrence.id)
        .join(Lesson, LessonOccurrence.lesson_id == Lesson.id)
        .join(Subject, Lesson.subject_id == Subject.id)
        .join(Membership, Attendance.member_id == Membership.id)
        .where(
            LessonOccurrence.group_id == group_id,
            LessonOccurrence.date >= date_from,
            LessonOccurrence.date <= date_to,
            LessonOccurrence.status == OccurrenceStatus.SCHEDULED,
        )
        .group_by(
            Lesson.subject_id,
            Subject.name,
            Lesson.subgroup,
            Membership.subgroup,
        )
    ).all()

    stats_by_subject: dict[int, dict] = {}
    for subject_id, name, lesson_subgroup, member_subgroup, absent, excused in rows:
        # Пропуск «чужой» лабораторной не должен попадать в статистику.
        if not lesson_fits_member(lesson_subgroup, member_subgroup):
            continue
        data = stats_by_subject.setdefault(
            subject_id, {"name": name, "absent": 0, "excused": 0}
        )
        data["absent"] += int(absent or 0)
        data["excused"] += int(excused or 0)

    totals_by_subject: dict[int, dict] = {}
    for subject_id, name, lesson_subgroup, count in occurrences:
        data = totals_by_subject.setdefault(
            subject_id, {"name": name, "total": 0, "absent": 0, "excused": 0}
        )
        data["total"] += int(count) * applicable_members(lesson_subgroup)

    result = []
    for subject_id, data in totals_by_subject.items():
        # Предмет без единого пропуска тоже должен попасть в отчёт: раньше
        # он исчезал, потому что строки Attendance для него не было.
        marks = stats_by_subject.get(subject_id, {"absent": 0, "excused": 0})
        total = data["total"]
        absent = marks["absent"]
        excused = marks["excused"]
        attended = max(0, total - absent - excused)
        base = attended + absent
        result.append(
            {
                "id": subject_id,
                "name": data["name"],
                "total": total,
                "attended": attended,
                "absent": absent,
                "excused": excused,
                "percent": round(attended * 100 / base) if base else None,
            }
        )
    result.sort(key=lambda item: item["name"])
    return result

def _document_students(doc) -> list[Membership]:
    students = list(getattr(doc, "students", []) or [])
    if students:
        return students
    if getattr(doc, "student", None) is not None:
        return [doc.student]
    return []

def _document_window(doc) -> tuple[datetime, datetime] | None:
    """Возвращает точный временной интервал действия документа."""
    if not doc.valid_from:
        return None
    start_day = doc.valid_from
    end_day = doc.valid_to or doc.valid_from
    start = datetime.combine(start_day, doc.valid_from_time or time.min)
    end = datetime.combine(end_day, doc.valid_to_time or time.max)
    return start, end

def apply_document(db: Session, doc) -> int:
    if doc.type not in (DocType.ORDER, DocType.CERTIFICATE):
        return 0
    if doc.status not in (DocStatus.ACTIVE, DocStatus.APPROVED):
        return 0

    students = _document_students(doc)
    window = _document_window(doc)
    if not students or window is None:
        doc.meta = {**(doc.meta or {}), "excused_lessons": 0}
        return 0

    start, end = window
    if start > end:
        doc.meta = {**(doc.meta or {}), "excused_lessons": 0}
        return 0

    stmt = select(LessonOccurrence).where(
        LessonOccurrence.group_id == doc.group_id,
        LessonOccurrence.date >= start.date(),
        LessonOccurrence.date <= end.date(),
        LessonOccurrence.status == OccurrenceStatus.SCHEDULED,
    )
    if doc.lesson_ids:
        stmt = stmt.where(LessonOccurrence.lesson_id.in_(doc.lesson_ids))

    occurrences = []
    for occurrence in db.scalars(stmt).all():
        occ_start = datetime.combine(occurrence.date, occurrence.lesson.start_time)
        occ_end = datetime.combine(occurrence.date, occurrence.lesson.end_time)
        # Ключевая проверка: пересекается ли время пары с временем действия документа
        if occ_start < end and occ_end > start:
            occurrences.append(occurrence)

    if not occurrences:
        doc.meta = {**(doc.meta or {}), "excused_lessons": 0}
        return 0

    student_ids = [student.id for student in students]
    member_by_id = {student.id: student for student in students}
    existing = {
        (row.member_id, row.occurrence_id): row
        for row in db.scalars(
            select(Attendance).where(
                Attendance.member_id.in_(student_ids),
                Attendance.occurrence_id.in_([o.id for o in occurrences]),
            )
        ).all()
    }

    label = DocType.LABELS.get(doc.type, doc.type)
    comment = f"{label} №{doc.number}" if doc.number else label
    changed = 0
    total = 0

    for student_id in student_ids:
        member = member_by_id.get(student_id)
        for occurrence in occurrences:
            if member is None or not lesson_fits_member(
                occurrence.lesson.subgroup, member.subgroup
            ):
                continue
            row = existing.get((student_id, occurrence.id))
            if row is None:
                row = Attendance(
                    occurrence_id=occurrence.id,
                    member_id=student_id,
                    status=AttendanceStatus.EXCUSED,
                    comment=comment,
                    source="document",
                    document_id=doc.id,
                    marked_at=now_local(),
                )
                db.add(row)
                changed += 1
                total += 1
                continue

            # Строку уже освободил другой документ или староста явно поставил
            # «присутствовал» — этот документ её не трогает.
            if row.status == AttendanceStatus.PRESENT:
                continue
            if row.status == AttendanceStatus.EXCUSED and row.document_id not in (None, doc.id):
                continue

            if row.status != AttendanceStatus.EXCUSED:
                row.previous_status = row.status or AttendanceStatus.ABSENT
            if row.status != AttendanceStatus.EXCUSED or row.document_id != doc.id:
                changed += 1
            row.status = AttendanceStatus.EXCUSED
            row.comment = comment
            row.source = "document"
            row.document_id = doc.id
            row.marked_at = now_local()
            total += 1

    db.flush()
    doc.meta = {**(doc.meta or {}), "excused_lessons": total}
    return changed

def revert_document(db: Session, doc) -> int:
    rows = db.scalars(
        select(Attendance).where(Attendance.document_id == doc.id)
    ).all()
    changed = 0
    for row in rows:
        if row.previous_status in (
            AttendanceStatus.PRESENT,
            AttendanceStatus.LATE,
            AttendanceStatus.NOT_ANSWERED,
        ):
            row.status = AttendanceStatus.PRESENT
        else:
            row.status = AttendanceStatus.ABSENT
        row.previous_status = None
        row.document_id = None
        row.source = "head"
        # Старый комментарий относился к документу и после отзыва/удаления
        # вводил бы в заблуждение.
        row.comment = None
        changed += 1
    db.flush()
    doc.meta = {**(doc.meta or {}), "excused_lessons": 0}
    return changed

def month_matrix(
    db: Session, group_id: int, year: int, month: int
) -> MonthData:
    first = date(year, month, 1)
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    last = next_month - timedelta(days=1)
    data = MonthData(year=year, month=month)
    members = roster(db, group_id)
    if not members:
        return data

    member_by_id = {member.id: member for member in members}
    occurrences = [
        occurrence
        for occurrence in list_range(db, group_id, first, last)
        if occurrence.status == OccurrenceStatus.SCHEDULED
    ]

    if not occurrences:
        data.students = [MonthStudent(member=member) for member in members]
        return data

    rows = db.scalars(
        select(Attendance).where(
            Attendance.occurrence_id.in_([o.id for o in occurrences]),
            Attendance.member_id.in_(list(member_by_id)),
        )
    ).all()

    statuses = {
        (row.member_id, row.occurrence_id): effective_status(row) for row in rows
    }

    students = {member.id: MonthStudent(member=member) for member in members}
    for occurrence in occurrences:
        hours = HOURS_PER_LESSON
        for member in members:
            if not lesson_fits_member(occurrence.lesson.subgroup, member.subgroup):
                continue
            status = statuses.get((member.id, occurrence.id), AttendanceStatus.PRESENT)
            if status not in (AttendanceStatus.ABSENT, AttendanceStatus.EXCUSED):
                continue

            item = students[member.id]
            item.days[occurrence.date.day] = (
                item.days.get(occurrence.date.day, 0) + hours
            )
            item.total_hours += hours
            if status == AttendanceStatus.EXCUSED:
                item.excused_hours += hours
            else:
                item.absent_hours += hours
                data.paid_entries.append(
                    PaidEntry(
                        member=member,
                        day=occurrence.date,
                        subject=occurrence.lesson.subject.name,
                        teacher=occurrence.lesson.teacher,
                        kind=occurrence.lesson.kind,
                        hours=hours,
                    )
                )

    data.students = [students[member.id] for member in members]
    return data

def last_sync_info(db: Session, group_id: int, term: int) -> dict | None:
    snapshot = latest_snapshot(db, group_id, term)
    if not snapshot:
        return None
    return {
        "updated": (snapshot.payload or {}).get("meta", {}).get("updated"),
        "fetched_at": snapshot.fetched_at,
        "lessons": snapshot.lessons_count,
        "weeks": snapshot.weeks_count,
    }
