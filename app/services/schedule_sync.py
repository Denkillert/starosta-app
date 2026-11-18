from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (
    Attendance,
    Group,
    Lesson,
    LessonOccurrence,
    ScheduleSnapshot,
    Subject,
    User,
)
from . import audit
from .polessu import (
    ParsedSchedule,
    ScheduleFetchError,
    ScheduleParseError,
    fetch_and_parse,
    fetch_html,
)

SNAPSHOTS_TO_KEEP = 10


@dataclass(slots=True)
class SyncResult:
    status: str
    changed: bool
    message: str
    group_id: int
    term: int
    lessons: int = 0
    weeks: int = 0
    created_lessons: int = 0
    updated_lessons: int = 0
    deactivated_lessons: int = 0
    occurrences_created: int = 0
    occurrences_cancelled: int = 0
    snapshot_id: int | None = None


def sync_group(
    db: Session,
    group: Group,
    term: int,
    *,
    fetcher=fetch_html,
    actor: User | None = None,
    force: bool = False,
) -> SyncResult:
    """Скачивает расписание, обновляет шаблоны пар и конкретные даты.

    Ручные отметки посещаемости и отмены не удаляются: если пара исчезла из
    расписания, но на ней уже есть отметки, она помечается отменённой.
    """

    try:
        parsed = fetch_and_parse(group.code, term, fetcher=fetcher)
    except (ScheduleFetchError, ScheduleParseError) as exc:
        snapshot = ScheduleSnapshot(
            group_id=group.id,
            term=term,
            source_url="",
            status="error",
            error=str(exc)[:2000],
        )
        db.add(snapshot)
        audit.log(
            db,
            action="schedule.sync_error",
            group_id=group.id,
            actor_id=actor.id if actor else None,
            entity_type="group",
            entity_id=group.id,
            payload={"term": term, "error": str(exc)[:500]},
        )
        db.commit()
        return SyncResult(
            status="error",
            changed=False,
            message=str(exc),
            group_id=group.id,
            term=term,
            snapshot_id=snapshot.id,
        )

    latest = db.scalar(
        select(ScheduleSnapshot)
        .where(
            ScheduleSnapshot.group_id == group.id,
            ScheduleSnapshot.term == term,
            ScheduleSnapshot.status == "ok",
        )
        .order_by(ScheduleSnapshot.id.desc())
        .limit(1)
    )
    if latest and latest.content_hash == parsed.content_hash and not force:
        return SyncResult(
            status="unchanged",
            changed=False,
            message="Расписание не изменилось с прошлой синхронизации.",
            group_id=group.id,
            term=term,
            lessons=latest.lessons_count,
            weeks=latest.weeks_count,
            snapshot_id=latest.id,
        )

    return import_parsed(db, group, term, parsed, actor=actor)


def import_parsed(
    db: Session,
    group: Group,
    term: int,
    parsed: ParsedSchedule,
    *,
    actor: User | None = None,
) -> SyncResult:
    """Сохраняет уже разобранное расписание (используется и демо-режимом)."""

    result = _apply_schedule(db, group, term, parsed)
    result.changed = True
    result.status = "ok"

    snapshot = ScheduleSnapshot(
        group_id=group.id,
        term=term,
        source_url=parsed.source_url,
        content_hash=parsed.content_hash,
        lessons_count=parsed.lesson_count,
        weeks_count=parsed.week_count,
        status="ok",
        payload={**parsed.to_payload(), "content_hash": parsed.content_hash},
    )
    db.add(snapshot)
    db.flush()
    result.snapshot_id = snapshot.id

    _prune_snapshots(db, group.id, term)

    audit.log(
        db,
        action="schedule.sync",
        group_id=group.id,
        actor_id=actor.id if actor else None,
        entity_type="group",
        entity_id=group.id,
        payload={
            "term": term,
            "lessons": parsed.lesson_count,
            "weeks": parsed.week_count,
            "occurrences_created": result.occurrences_created,
            "occurrences_cancelled": result.occurrences_cancelled,
            "created": result.created_lessons,
            "updated": result.updated_lessons,
            "deactivated": result.deactivated_lessons,
        },
    )
    db.commit()

    result.message = (
        f"Импортировано {parsed.lesson_count} строк расписания "
        f"на {parsed.week_count} недель."
    )
    return result


def _apply_schedule(
    db: Session, group: Group, term: int, parsed: ParsedSchedule
) -> SyncResult:
    result = SyncResult(
        status="ok",
        changed=False,
        message="",
        group_id=group.id,
        term=term,
        lessons=parsed.lesson_count,
        weeks=parsed.week_count,
    )

    subjects = {s.name: s for s in db.scalars(select(Subject)).all()}
    existing_lessons = {
        lesson.source_key: lesson
        for lesson in db.scalars(
            select(Lesson).where(Lesson.group_id == group.id, Lesson.term == term)
        ).all()
    }

    seen_keys: set[str] = set()
    lesson_by_key: dict[str, Lesson] = {}

    for item in parsed.lessons:
        subject = subjects.get(item.subject)
        if subject is None:
            subject = Subject(name=item.subject)
            db.add(subject)
            db.flush()
            subjects[item.subject] = subject

        key = item.source_key
        seen_keys.add(key)
        lesson = existing_lessons.get(key)
        if lesson is None:
            lesson = Lesson(
                group_id=group.id,
                subject_id=subject.id,
                term=term,
                day_of_week=item.day_of_week,
                start_time=item.start_time,
                end_time=item.end_time,
                kind=item.kind,
                room=item.room,
                teacher=item.teacher,
                subgroup=item.subgroup,
                weeks=item.weeks,
                source_key=key,
                is_active=True,
            )
            db.add(lesson)
            db.flush()
            existing_lessons[key] = lesson
            result.created_lessons += 1
        else:
            if lesson.weeks != item.weeks or not lesson.is_active:
                result.updated_lessons += 1
            lesson.subject_id = subject.id
            lesson.weeks = item.weeks
            lesson.is_active = True
        lesson_by_key[key] = lesson

    for key, lesson in existing_lessons.items():
        if key not in seen_keys and lesson.is_active:
            lesson.is_active = False
            result.deactivated_lessons += 1

    db.flush()

    # ----- конкретные даты -------------------------------------------------- #
    lesson_ids = [lesson.id for lesson in existing_lessons.values()]
    existing_occurrences: dict[tuple[int, object], LessonOccurrence] = {}
    if lesson_ids:
        for occurrence in db.scalars(
            select(LessonOccurrence).where(
                LessonOccurrence.lesson_id.in_(lesson_ids)
            )
        ).all():
            existing_occurrences[(occurrence.lesson_id, occurrence.date)] = occurrence

    desired: set[tuple[int, object]] = set()
    for item in parsed.lessons:
        lesson = lesson_by_key[item.source_key]
        for day in item.dates:
            desired.add((lesson.id, day))
            if (lesson.id, day) not in existing_occurrences:
                db.add(
                    LessonOccurrence(
                        lesson_id=lesson.id,
                        group_id=group.id,
                        date=day,
                        status="scheduled",
                    )
                )
                result.occurrences_created += 1

    removed = [
        (key, occurrence)
        for key, occurrence in existing_occurrences.items()
        if key not in desired and occurrence.status != "cancelled"
    ]
    if removed:
        removed_ids = [occ.id for _, occ in removed]
        marked = dict(
            db.execute(
                select(Attendance.occurrence_id, func.count(Attendance.id))
                .where(Attendance.occurrence_id.in_(removed_ids))
                .group_by(Attendance.occurrence_id)
            ).all()
        )
        for _, occurrence in removed:
            if marked.get(occurrence.id):
                occurrence.status = "cancelled"
                occurrence.note = "Пара исчезла из расписания вуза"
                result.occurrences_cancelled += 1
            else:
                db.delete(occurrence)

    db.flush()
    return result


def _prune_snapshots(db: Session, group_id: int, term: int) -> None:
    ids = db.scalars(
        select(ScheduleSnapshot.id)
        .where(ScheduleSnapshot.group_id == group_id, ScheduleSnapshot.term == term)
        .order_by(ScheduleSnapshot.id.desc())
        .offset(SNAPSHOTS_TO_KEEP)
    ).all()
    if ids:
        db.execute(delete(ScheduleSnapshot).where(ScheduleSnapshot.id.in_(ids)))


def latest_snapshot(db: Session, group_id: int, term: int) -> ScheduleSnapshot | None:
    return db.scalar(
        select(ScheduleSnapshot)
        .where(
            ScheduleSnapshot.group_id == group_id,
            ScheduleSnapshot.term == term,
            ScheduleSnapshot.status == "ok",
        )
        .order_by(ScheduleSnapshot.id.desc())
        .limit(1)
    )
