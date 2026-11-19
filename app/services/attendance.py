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
    Role,
)
from ..models import Attendance, Lesson, LessonOccurrence, Membership, Subject, User
from ..utils import now_local
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

def roster(db: Session, group_id: int) -> list[Membership]:
    role_order = case(
        (Membership.role == Role.HEAD, 0),
        (Membership.role == Role.DEPUTY, 1),
        else_=2,
    )
    return list(
        db.scalars(
            select(Membership)
            .where(
                Membership.group_id == group_id,
                Membership.status == MemberStatus.ACTIVE,
            )
            .order_by(role_order, Membership.full_name)
        ).all()
    )

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
        for member in members:
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

    total_occurrences = db.scalar(
        select(func.count(LessonOccurrence.id)).where(
            LessonOccurrence.group_id == group_id,
            LessonOccurrence.date >= date_from,
            LessonOccurrence.date <= date_to,
            LessonOccurrence.status == OccurrenceStatus.SCHEDULED,
        )
    ) or 0

    rows = db.execute(
        select(Attendance.member_id, Attendance.status, func.count(Attendance.id))
        .join(LessonOccurrence, Attendance.occurrence_id == LessonOccurrence.id)
        .where(
            LessonOccurrence.group_id == group_id,
            LessonOccurrence.date >= date_from,
            LessonOccurrence.date <= date_to,
            LessonOccurrence.status == OccurrenceStatus.SCHEDULED,
            Attendance.member_id.in_(list(stats.keys())),
        )
        .group_by(Attendance.member_id, Attendance.status)
    ).all()

    raw: dict[int, dict[str, int]] = {}
    for member_id, status, count in rows:
        raw.setdefault(member_id, {})[status] = count

    for member_id, item in stats.items():
        counts = raw.get(member_id, {})
        item.absent = counts.get(AttendanceStatus.ABSENT, 0)
        item.excused = counts.get(AttendanceStatus.EXCUSED, 0)
        item.total = total_occurrences
        item.present = max(0, total_occurrences - item.absent - item.excused)

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
    members_count = len(roster(db, group_id))
    if not members_count:
        return []

    occurrences = db.execute(
        select(Lesson.subject_id, Subject.name, func.count(LessonOccurrence.id))
        .select_from(LessonOccurrence)
        .join(Lesson, LessonOccurrence.lesson_id == Lesson.id)
        .join(Subject, Lesson.subject_id == Subject.id)
        .where(
            LessonOccurrence.group_id == group_id,
            LessonOccurrence.date >= date_from,
            LessonOccurrence.date <= date_to,
            LessonOccurrence.status == OccurrenceStatus.SCHEDULED,
        )
        .group_by(Lesson.subject_id, Subject.name)
    ).all()

    rows = db.execute(
        select(
            Lesson.subject_id,
            Subject.name,
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
        .where(
            LessonOccurrence.group_id == group_id,
            LessonOccurrence.date >= date_from,
            LessonOccurrence.date <= date_to,
            LessonOccurrence.status == OccurrenceStatus.SCHEDULED,
        )
        .group_by(Lesson.subject_id, Subject.name)
    ).all()

    stats_by_subject = {
        subject_id: {
            "name": name,
            "absent": int(absent or 0),
            "excused": int(excused or 0),
        }
        for subject_id, name, absent, excused in rows
    }

    result = []
    for subject_id, name, count in occurrences:
        # Предмет без единого пропуска тоже должен попасть в отчёт: раньше
        # он исчезал, потому что строки Attendance для него не было.
        data = stats_by_subject.get(subject_id, {"absent": 0, "excused": 0})
        total = int(count) * members_count
        absent = data["absent"]
        excused = data["excused"]
        attended = max(0, total - absent - excused)
        base = attended + absent
        result.append(
            {
                "id": subject_id,
                "name": name,
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
        for occurrence in occurrences:
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
