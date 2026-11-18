from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.constants import OccurrenceStatus
from app.models import Group, LessonOccurrence, User
from app.services import schedule_sync

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fetcher(_url: str) -> str:
    return (FIXTURES / "ruz_24it2_term1.html").read_text(encoding="utf-8")


def test_reimport_is_idempotent_and_unchanged(db, seeded):
    group = db.get(Group, seeded["group_id"])
    user = db.get(User, seeded["user_id"])

    result = schedule_sync.sync_group(
        db, group, 1, fetcher=_fetcher, actor=user, force=True
    )
    assert result.status == "ok"
    assert result.lessons == 324
    assert result.weeks == 16
    assert result.occurrences_created == 0
    assert result.created_lessons == 0

    again = schedule_sync.sync_group(db, group, 1, fetcher=_fetcher, actor=user)
    assert again.status == "unchanged"
    assert again.changed is False


def test_manual_cancel_survives_resync(db, seeded):
    group = db.get(Group, seeded["group_id"])
    occurrence = db.scalar(
        select(LessonOccurrence)
        .where(LessonOccurrence.group_id == group.id)
        .order_by(LessonOccurrence.date)
        .limit(1)
    )
    occurrence.status = OccurrenceStatus.CANCELLED
    occurrence.note = "Отменено старостой"
    db.commit()

    schedule_sync.sync_group(db, group, 1, fetcher=_fetcher, force=True)
    db.refresh(occurrence)
    assert occurrence.status == OccurrenceStatus.CANCELLED
    assert occurrence.note == "Отменено старостой"


def test_fetch_error_is_reported(db, seeded):
    group = db.get(Group, seeded["group_id"])

    def broken(_url: str) -> str:
        raise schedule_sync.ScheduleFetchError("сеть недоступна")

    result = schedule_sync.sync_group(db, group, 1, fetcher=broken, force=True)
    assert result.status == "error"
    assert "сеть недоступна" in result.message
