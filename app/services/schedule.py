from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..models import Group, Lesson, LessonOccurrence, ScheduleSnapshot
from .schedule_sync import latest_snapshot


def _occurrence_query():
    return (
        select(LessonOccurrence)
        .join(Lesson, LessonOccurrence.lesson_id == Lesson.id)
        .options(joinedload(LessonOccurrence.lesson).joinedload(Lesson.subject))
    )


def list_range(
    db: Session, group_id: int, date_from: date, date_to: date
) -> list[LessonOccurrence]:
    return list(
        db.scalars(
            _occurrence_query()
            .where(
                LessonOccurrence.group_id == group_id,
                LessonOccurrence.date >= date_from,
                LessonOccurrence.date <= date_to,
            )
            .order_by(
                LessonOccurrence.date,
                Lesson.start_time,
                Lesson.subgroup,
                Lesson.id,
            )
        ).all()
    )


def list_day(db: Session, group_id: int, day: date) -> list[LessonOccurrence]:
    return list_range(db, group_id, day, day)


def list_week(
    db: Session, group_id: int, day: date
) -> tuple[date, date, dict[date, list[LessonOccurrence]]]:
    start = day - timedelta(days=day.weekday())
    end = start + timedelta(days=6)
    grouped: dict[date, list[LessonOccurrence]] = {start + timedelta(days=i): [] for i in range(7)}
    for occurrence in list_range(db, group_id, start, end):
        grouped[occurrence.date].append(occurrence)
    return start, end, grouped


def terms_for_date(db: Session, group: Group, day: date) -> int:
    snapshots = db.scalars(
        select(ScheduleSnapshot)
        .where(
            ScheduleSnapshot.group_id == group.id,
            ScheduleSnapshot.status == "ok",
        )
        .order_by(ScheduleSnapshot.id.desc())
    ).all()
    seen: set[int] = set()
    for snapshot in snapshots:
        if snapshot.term in seen:
            continue
        seen.add(snapshot.term)
        weeks: list[dict] = []
        for week in (snapshot.payload or {}).get("weeks", []):
            try:
                weeks.append(
                    {
                        "start": date.fromisoformat(week["start"]),
                        "end": date.fromisoformat(week["end"]),
                    }
                )
            except (KeyError, ValueError):
                continue
        semester_start = _weeks_semester_start(weeks)
        for week in weeks:
            if _week_contains(week, day, semester_start):
                return snapshot.term
    return group.current_term


def list_weeks(db: Session, group_id: int, term: int) -> list[dict]:
    snapshot = latest_snapshot(db, group_id, term)
    weeks: list[dict] = []
    if snapshot and snapshot.payload:
        for item in snapshot.payload.get("weeks", []):
            try:
                weeks.append(
                    {
                        "number": int(item["number"]),
                        "start": date.fromisoformat(item["start"]),
                        "end": date.fromisoformat(item["end"]),
                    }
                )
            except (KeyError, ValueError):
                continue
    if weeks:
        return weeks

    occurrences = list_range(db, group_id, date(2000, 1, 1), date(2100, 1, 1))
    if not occurrences:
        return []
    first = min(o.date for o in occurrences)
    last = max(o.date for o in occurrences)
    start = first - timedelta(days=first.weekday())
    number = 1
    while start <= last:
        weeks.append({"number": number, "start": start, "end": start + timedelta(days=5)})
        start += timedelta(days=7)
        number += 1
    return weeks


def _weeks_semester_start(weeks: list[dict]) -> date | None:
    return min((week["start"] for week in weeks), default=None)


def _week_contains(week: dict, day: date, semester_start: date | None) -> bool:
    # Сайт ПолесГУ нумерует недели со вторника по воскресенье, поэтому
    # понедельник относится к той же неделе, что и её вторник. Исключение —
    # понедельник до начала семестра: он не должен попадать в первую неделю.
    if week["start"] <= day <= week["end"]:
        return True
    return bool(semester_start and semester_start <= day < week["start"])


def week_for_date(weeks: list[dict], day: date) -> dict | None:
    semester_start = _weeks_semester_start(weeks)
    for week in weeks:
        if _week_contains(week, day, semester_start):
            return week
    return None
