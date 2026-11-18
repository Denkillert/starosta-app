from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models import Group
from app.services import schedule_sync, scheduler
from app.utils import parse_date

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fetcher(_url: str) -> str:
    return (FIXTURES / "ruz_24it2_term1.html").read_text(encoding="utf-8")


def _group(db, seeded) -> Group:
    group = db.get(Group, seeded["group_id"])
    assert group is not None
    return group


def _first_day(seeded):
    return parse_date(seeded["first_date"])


def test_fresh_snapshot_is_not_due(db, seeded):
    group = _group(db, seeded)
    assert scheduler.due_terms(db, group, hours=6, day=_first_day(seeded)) == []


def test_stale_snapshot_is_due(db, seeded):
    group = _group(db, seeded)
    snapshot = schedule_sync.latest_snapshot(db, group.id, 1)
    assert snapshot is not None
    snapshot.fetched_at = datetime.now(UTC) - timedelta(hours=48)
    db.commit()

    assert scheduler.due_terms(db, group, hours=6, day=_first_day(seeded)) == [1]


def test_sync_due_groups_updates_requested_term(db, seeded):
    group = _group(db, seeded)
    snapshot = schedule_sync.latest_snapshot(db, group.id, 1)
    assert snapshot is not None
    snapshot.fetched_at = datetime.now(UTC) - timedelta(hours=48)
    db.commit()

    outcomes = scheduler.sync_due_groups(
        db, fetcher=_fetcher, terms=[1], delay=0, hours=6
    )
    assert len(outcomes) == 1
    assert outcomes[0].code == "24ИТ-2"
    assert outcomes[0].term == 1
    # Содержимое не изменилось — повторный импорт не нужен.
    assert outcomes[0].status == "unchanged"

    forced = scheduler.sync_due_groups(
        db, fetcher=_fetcher, terms=[1], delay=0, force=True
    )
    assert len(forced) == 1
    assert forced[0].status == "ok"


def test_run_once_records_state(session_factory, seeded, monkeypatch):
    monkeypatch.setattr(scheduler, "SessionLocal", session_factory)

    outcomes = scheduler.run_once(fetcher=_fetcher, force=True, terms=[1], delay=0)
    assert outcomes

    status = scheduler.status()
    assert status["last_finished_at"] is not None
    assert status["last_error"] is None
    assert status["last_outcomes"][-1]["code"] == "24ИТ-2"


def test_scheduler_status_endpoint(api_client, seeded):
    response = api_client.get("/api/scheduler")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["enabled"] is False  # отключено в conftest
    assert data["interval_minutes"] >= 1
    assert "last_error" in data
