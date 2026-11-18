from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .config import BASE_DIR, settings
from .constants import (
    AttendanceStatus,
    DocStatus,
    DocType,
    Funding,
    MemberStatus,
    OccurrenceStatus,
    Role,
)
from .models import Group, Membership, User
from .services.attendance import effective_status
from .services.groups import memberships_for_user
from .utils import (
    RU_WEEKDAYS,
    RU_WEEKDAYS_SHORT,
    format_date,
    format_date_short,
    format_period,
    format_time,
    short_name,
)

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _status_class(value: str) -> str:
    return {
        AttendanceStatus.PRESENT: "ok",
        AttendanceStatus.LATE: "warn",
        AttendanceStatus.ABSENT: "bad",
        AttendanceStatus.EXCUSED: "info",
        AttendanceStatus.NOT_ANSWERED: "muted",
    }.get(value, "muted")


def _doc_status_class(value: str) -> str:
    return {
        DocStatus.ACTIVE: "ok",
        DocStatus.APPROVED: "ok",
        DocStatus.PENDING: "warn",
        DocStatus.DRAFT: "muted",
        DocStatus.REVOKED: "bad",
        DocStatus.REJECTED: "bad",
        DocStatus.ARCHIVED: "muted",
    }.get(value, "muted")


def _percent_class(value: int | None) -> str:
    if value is None:
        return "muted"
    if value >= 75:
        return "ok"
    if value >= 50:
        return "warn"
    return "bad"


templates.env.filters.update(
    {
        "date_ru": format_date,
        "date_short": format_date_short,
        "period": format_period,
        "hm": format_time,
        "status_label": lambda v: AttendanceStatus.LABELS.get(v, v),
        "status_class": _status_class,
        "doc_label": lambda v: DocType.LABELS.get(v, v),
        "doc_status_label": lambda v: DocStatus.LABELS.get(v, v),
        "doc_status_class": _doc_status_class,
        "role_label": lambda v: Role.LABELS.get(v, v),
        "member_status_label": lambda v: MemberStatus.LABELS.get(v, v),
        "occurrence_label": lambda v: OccurrenceStatus.LABELS.get(v, v),
        "weekday": lambda i: RU_WEEKDAYS[i] if 0 <= i <= 6 else "",
        "weekday_short": lambda i: RU_WEEKDAYS_SHORT[i] if 0 <= i <= 6 else "",
        "percent_class": _percent_class,
        "iso": lambda d: d.isoformat() if d else "",
        "short_name": short_name,
        "effective_status": effective_status,
        "status_symbol": lambda v: {
            AttendanceStatus.PRESENT: "",
            AttendanceStatus.ABSENT: "Н",
            AttendanceStatus.EXCUSED: "У",
        }.get(v, ""),
        "funding_label": lambda v: Funding.LABELS.get(v, v),
        "funding_short": lambda v: Funding.SHORT.get(v, v),
    }
)


def url_with(path: str, **params) -> str:
    clean = {key: value for key, value in params.items() if value is not None}
    return f"{path}?{urlencode(clean)}" if clean else path


templates.env.globals["url_with"] = url_with
templates.env.globals.update(
    {
        "DocType": DocType,
        "DocStatus": DocStatus,
        "AttendanceStatus": AttendanceStatus,
        "Role": Role,
        "MemberStatus": MemberStatus,
        "Funding": Funding,
        "settings": settings,
        "app_name": settings.app_name,
    }
)


@dataclass
class WebContext:
    user: User
    groups: list[tuple[Group, Membership]]
    group: Group | None
    membership: Membership | None
    page: str
    term: int
    flash: str | None = None
    error: str | None = None

    @property
    def is_staff(self) -> bool:
        return bool(self.membership and self.membership.role in Role.STAFF)

    @property
    def group_id(self) -> int | None:
        return self.group.id if self.group else None


def build_context(
    request: Request,
    db: Session,
    user: User,
    *,
    page: str,
    group_id: int | None = None,
    term: int | None = None,
) -> WebContext:
    pairs: list[tuple[Group, Membership]] = []
    for membership in memberships_for_user(db, user.id):
        if membership.group is not None:
            pairs.append((membership.group, membership))

    requested = group_id
    if requested is None:
        raw = request.query_params.get("group")
        if raw and raw.isdigit():
            requested = int(raw)

    selected: tuple[Group, Membership] | None = None
    if requested is not None:
        selected = next((p for p in pairs if p[0].id == requested), None)
    if selected is None and pairs:
        selected = pairs[0]

    group = selected[0] if selected else None
    membership = selected[1] if selected else None

    raw_term = term if term is not None else request.query_params.get("term")
    try:
        term_value = int(raw_term) if raw_term is not None else None
    except (TypeError, ValueError):
        term_value = None
    if term_value not in (1, 2):
        term_value = group.current_term if group else 1

    return WebContext(
        user=user,
        groups=pairs,
        group=group,
        membership=membership,
        page=page,
        term=term_value,
        flash=request.query_params.get("msg"),
        error=request.query_params.get("err"),
    )


def render(request: Request, name: str, context: WebContext, **extra):
    payload = {"ctx": context, "request": request, **extra}
    return templates.TemplateResponse(request, name, payload)
