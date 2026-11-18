from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Request

from ..deps import CurrentUser, DbSession, group_for_user
from ..services import attendance as attendance_service
from ..services import schedule as schedule_service
from ..services import scheduler
from ..utils import parse_date, today_local


router = APIRouter(prefix="/api", include_in_schema=False)


@router.get("/me")
def me(user: CurrentUser) -> dict:
    return {"user": {"telegram_id": user.telegram_id, "full_name": user.full_name}}


@router.get("/scheduler")
def scheduler_status() -> dict:
    return scheduler.status()


@router.get("/groups/{group_id}/schedule")
def group_schedule(
    group_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    group, _ = group_for_user(db, user, group_id)
    day = parse_date(request.query_params.get("date"), today_local())
    days = int(request.query_params.get("days", 1) or 1)
    out = []
    for offset in range(max(1, days)):
        current = day + timedelta(days=offset)
        out.append({
            "date": current.isoformat(),
            "occurrences": [
                {"id": o.id}
                for o in schedule_service.list_day(db, group.id, current)
            ],
        })
    return {"days": out}


@router.get("/groups/{group_id}/journal")
def group_journal(
    group_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    group, _ = group_for_user(db, user, group_id)
    day = parse_date(request.query_params.get("date"), today_local())
    views = attendance_service.day_overview(db, group.id, day)
    return {
        "occurrences": [
            {
                "occurrence_id": view.occurrence.id,
                "entries": [
                    {"member_id": m, "status": a.status}
                    for m, a in view.marks.items()
                ],
            }
            for view in views
        ]
    }