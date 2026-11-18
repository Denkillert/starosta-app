from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Group, User
from ..utils import TZ, today_local
from . import schedule as schedule_service
from . import schedule_sync
from .polessu import fetch_html

logger = logging.getLogger(__name__)

Fetcher = Callable[[str], str]


@dataclass(slots=True)
class SyncOutcome:
    group_id: int
    code: str
    term: int
    status: str
    message: str


@dataclass(slots=True)
class SchedulerState:
    running: bool = False
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_error: str | None = None
    last_outcomes: list[dict[str, Any]] = field(default_factory=list)


STATE = SchedulerState()


def interval_minutes() -> int:
    if settings.scheduler_interval_minutes > 0:
        return max(1, settings.scheduler_interval_minutes)
    return max(1, settings.schedule_sync_hours * 60)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def due_terms(
    db: Session,
    group: Group,
    *,
    hours: int | None = None,
    day=None,
) -> list[int]:
    """Семестры, которые давно не синхронизировались (или не синхронизировались вовсе)."""
    window = max(1, hours or settings.schedule_sync_hours)
    target_day = day or today_local()
    threshold = _utcnow() - timedelta(hours=window)
    candidates = {
        group.current_term or 1,
        schedule_service.terms_for_date(db, group, target_day),
    }

    due: list[int] = []
    for term in sorted(term for term in candidates if term in (1, 2)):
        snapshot = schedule_sync.latest_snapshot(db, group.id, term)
        if snapshot is None or _as_utc(snapshot.fetched_at) <= threshold:
            due.append(term)
    return due


def sync_due_groups(
    db: Session,
    *,
    fetcher: Fetcher = fetch_html,
    hours: int | None = None,
    actor: User | None = None,
    delay: float | None = None,
    force: bool = False,
    terms: list[int] | None = None,
) -> list[SyncOutcome]:
    """Синхронизирует устаревшие семестры всех групп с паузой между запросами."""
    pause = settings.polessu_crawl_delay_seconds if delay is None else delay
    groups = list(db.scalars(select(Group).order_by(Group.id)).all())
    outcomes: list[SyncOutcome] = []
    requested = False

    for group in groups:
        if terms:
            targets = [term for term in terms if term in (1, 2)]
        elif force:
            targets = sorted(
                {
                    group.current_term or 1,
                    schedule_service.terms_for_date(db, group, today_local()),
                }
            )
        else:
            targets = due_terms(db, group, hours=hours)

        for term in targets:
            if requested and pause > 0:
                time.sleep(pause)
            requested = True
            result = schedule_sync.sync_group(
                db, group, term, fetcher=fetcher, actor=actor, force=force
            )
            outcomes.append(
                SyncOutcome(
                    group_id=group.id,
                    code=group.code,
                    term=term,
                    status=result.status,
                    message=result.message,
                )
            )
            logger.info(
                "scheduler: %s семестр %s → %s (%s)",
                group.code,
                term,
                result.status,
                result.message,
            )
    return outcomes


def run_once(
    *,
    fetcher: Fetcher = fetch_html,
    hours: int | None = None,
    force: bool = False,
    terms: list[int] | None = None,
    delay: float | None = None,
) -> list[SyncOutcome]:
    """Один проход планировщика. Не запускается параллельно с другим проходом."""
    if STATE.running:
        logger.warning("scheduler: предыдущий проход ещё выполняется, пропускаю")
        return []

    STATE.running = True
    STATE.last_started_at = _utcnow()
    try:
        with SessionLocal() as db:
            outcomes = sync_due_groups(
                db,
                fetcher=fetcher,
                hours=hours,
                force=force,
                terms=terms,
                delay=delay,
            )
        STATE.last_outcomes = [asdict(outcome) for outcome in outcomes]
        STATE.last_error = None
        if outcomes:
            logger.info("scheduler: обработано запросов — %s", len(outcomes))
        return outcomes
    except Exception as exc:
        STATE.last_error = str(exc)[:500]
        logger.exception("scheduler: проход завершился ошибкой")
        return []
    finally:
        STATE.running = False
        STATE.last_finished_at = _utcnow()


async def loop() -> None:
    """Фоновый цикл внутри процесса веб-приложения."""
    startup = max(0, settings.scheduler_startup_delay_seconds)
    if startup:
        await asyncio.sleep(startup)

    while True:
        await asyncio.to_thread(run_once)
        await asyncio.sleep(interval_minutes() * 60)


def _local(value: datetime | None) -> str | None:
    return value.astimezone(TZ).strftime("%d.%m.%Y %H:%M") if value else None


def status() -> dict[str, Any]:
    return {
        "enabled": settings.scheduler_enabled,
        "interval_minutes": interval_minutes(),
        "sync_hours": settings.schedule_sync_hours,
        "crawl_delay_seconds": settings.polessu_crawl_delay_seconds,
        "running": STATE.running,
        "last_started_at": STATE.last_started_at.isoformat()
        if STATE.last_started_at
        else None,
        "last_finished_at": STATE.last_finished_at.isoformat()
        if STATE.last_finished_at
        else None,
        "last_finished_local": _local(STATE.last_finished_at),
        "last_error": STATE.last_error,
        "last_outcomes": STATE.last_outcomes[-20:],
    }
