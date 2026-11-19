from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from ..config import settings
from ..constants import (
    AttendanceStatus,
    DocStatus,
    DocType,
    OccurrenceStatus,
    Role,
)
from ..deps import CurrentUser, DbSession, OptionalUser
from ..models import Document, LessonOccurrence, Membership, User
from ..security import (
    MARKER_COOKIE,
    SESSION_COOKIE,
    SESSION_TTL,
    session_secret,
    sign_file_token,
    sign_session,
    user_from_init_data,
    validate_init_data,
    verify_file_token,
)
from ..services import attendance as attendance_service
from ..services import audit, schedule_sync
from ..services import demo as demo_service
from ..services import documents as documents_service
from ..services import group_db as group_db_service
from ..services import groups as groups_service
from ..services import schedule as schedule_service
from ..services import vedomosti as vedomosti_service
from ..utils import (
    format_period,
    now_local,
    parse_date,
    parse_int,
    today_local,
)
from ..web import WebContext, build_context, render

router = APIRouter(include_in_schema=False)


def _safe_next(value: str | None, fallback: str = "/") -> str:
    """Разрешает только локальный путь без перехода на чужой сайт.

    «/\\evil.com» и «//evil.com» браузер понимает как внешний адрес, поэтому
    такие значения и управляющие символы отбрасываем.
    """
    if not value:
        return fallback
    value = value.strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return fallback
    if any(char in value for char in "\r\n\t"):
        return fallback
    return value


def _redirect(url: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    if msg or err:
        params = {}
        if msg:
            params["msg"] = msg
        if err:
            params["err"] = err
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    return RedirectResponse(url, status_code=303)


def _form_next(form, fallback: str) -> str:
    return _safe_next(form.get("next"), fallback)


def _form_group_id(form) -> int | None:
    """Группа из скрытого поля формы: без него build_context возьмёт первую."""
    raw = str(form.get("group") or "")
    return int(raw) if raw.isdigit() else None


def _back(fallback: str, ctx) -> str:
    return f"{fallback}?group={ctx.group.id}" if ctx.group else fallback


def _ensure_staff(ctx) -> bool:
    return bool(ctx.is_staff)


def _parse_month(value: str | None, fallback: date | None = None) -> tuple[int, int]:
    base = fallback or today_local()
    if value:
        for fmt in ("%Y-%m", "%m.%Y"):
            try:
                parsed = datetime.strptime(value, fmt)
                return parsed.year, parsed.month
            except ValueError:
                continue
        # «2026-10» уже покрыт; на всякий случай принимаем только год-месяц.
        parts = value.replace(".", "-").split("-")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            year, month = int(parts[0]), int(parts[1])
            if 1 <= year <= 9999 and 1 <= month <= 12:
                return year, month
    return base.year, base.month


def _month_value(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _parse_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _docx_response(data: bytes, filename: str) -> Response:
    return Response(
        content=data,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"report.docx\"; "
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@router.get("/")
def dashboard(request: Request, db: DbSession, user: CurrentUser):
    ctx = build_context(request, db, user, page="dashboard")
    if ctx.group is None:
        return render(request, "onboarding.html", ctx)

    today = today_local()
    term = schedule_service.terms_for_date(db, ctx.group, today)
    ctx.term = term
    weeks = schedule_service.list_weeks(db, ctx.group.id, term)
    week = schedule_service.week_for_date(weeks, today)

    views = attendance_service.day_overview(db, ctx.group.id, today, terms=term)
    now = now_local().time()
    upcoming = [
        view
        for view in views
        if view.occurrence.status == OccurrenceStatus.SCHEDULED
        and view.occurrence.lesson.end_time >= now
    ]
    tomorrow = today + timedelta(days=1)
    tomorrow_lessons = [
        o
        for o in schedule_service.list_day(db, ctx.group.id, tomorrow)
        if o.lesson.term == term
        and o.status != OccurrenceStatus.CANCELLED
    ]

    month_start = today.replace(day=1)
    stats = attendance_service.summary(db, ctx.group.id, month_start, today)
    totals = attendance_service.totals(stats)

    doc_counts = documents_service.counts_by_type(db, ctx.group.id)
    pending_statements = (
        documents_service.list_documents(
            db,
            ctx.group.id,
            type_=DocType.STATEMENT,
            status=DocStatus.PENDING,
            limit=5,
        )
        if ctx.is_staff
        else []
    )

    return render(
        request,
        "dashboard.html",
        ctx,
        today=today,
        tomorrow=tomorrow,
        term=term,
        week=week,
        views=views,
        upcoming=upcoming,
        tomorrow_lessons=tomorrow_lessons,
        stats=stats,
        totals=totals,
        members_count=len(stats),
        doc_counts=doc_counts,
        pending_statements=pending_statements,
        last_sync=attendance_service.last_sync_info(db, ctx.group.id, term),
        now=now_local(),
    )


@router.get("/schedule")
def schedule_page(request: Request, db: DbSession, user: CurrentUser):
    ctx = build_context(request, db, user, page="schedule")
    if ctx.group is None:
        return render(request, "onboarding.html", ctx)

    today = today_local()
    day = parse_date(request.query_params.get("date"), today)
    term = parse_int(request.query_params.get("term"))
    if term not in (1, 2):
        term = schedule_service.terms_for_date(db, ctx.group, day)
    ctx.term = term

    start, end, grouped = schedule_service.list_week(db, ctx.group.id, day)
    weeks = schedule_service.list_weeks(db, ctx.group.id, term)
    week = schedule_service.week_for_date(weeks, day)
    days = [
        {
            "date": start + timedelta(days=offset),
            "lessons": [
                o
                for o in grouped[start + timedelta(days=offset)]
                if o.lesson.term == term
                and o.status != OccurrenceStatus.CANCELLED
            ],
        }
        for offset in range(7)
    ]

    term_options = []
    for value in (1, 2):
        term_weeks = schedule_service.list_weeks(db, ctx.group.id, value)
        if term_weeks:
            term_options.append(
                {
                    "term": value,
                    "start": term_weeks[0]["start"],
                    "end": term_weeks[-1]["end"],
                }
            )

    return render(
        request,
        "schedule.html",
        ctx,
        day=day,
        start=start,
        end=end,
        days=days,
        week=week,
        weeks=weeks,
        today=today,
        term=term,
        term_options=term_options,
        prev_week=day - timedelta(days=7),
        next_week=day + timedelta(days=7),
        last_sync=attendance_service.last_sync_info(db, ctx.group.id, term),
    )


@router.get("/journal")
def journal_page(request: Request, db: DbSession, user: CurrentUser):
    ctx = build_context(request, db, user, page="journal")
    if ctx.group is None:
        return render(request, "onboarding.html", ctx)

    today = today_local()
    day = parse_date(request.query_params.get("date"), today)
    term = parse_int(request.query_params.get("term"))
    if term not in (1, 2):
        term = schedule_service.terms_for_date(db, ctx.group, day)
    ctx.term = term

    views = attendance_service.day_overview(db, ctx.group.id, day, terms=term)
    roster = attendance_service.roster(db, ctx.group.id)

    return render(
        request,
        "journal.html",
        ctx,
        day=day,
        views=views,
        roster=roster,
        prev_day=day - timedelta(days=1),
        next_day=day + timedelta(days=1),
        today=today,
    )


@router.get("/students")
def students_page(request: Request, db: DbSession, user: CurrentUser):
    ctx = build_context(request, db, user, page="students")
    if ctx.group is None:
        return render(request, "onboarding.html", ctx)

    today = today_local()
    month_start = today.replace(day=1)
    stats = attendance_service.summary(db, ctx.group.id, month_start, today)
    stats_by_member = {item.member.id: item for item in stats}
    members = groups_service.group_members(db, ctx.group.id)

    return render(
        request,
        "students.html",
        ctx,
        members=members,
        stats_by_member=stats_by_member,
        month_start=month_start,
        today=today,
        db_token=sign_file_token(user_id=user.id, group_id=ctx.group.id),
    )


@router.get("/documents")
def documents_page(request: Request, db: DbSession, user: CurrentUser):
    ctx = build_context(request, db, user, page="documents")
    if ctx.group is None:
        return render(request, "onboarding.html", ctx)

    type_ = request.query_params.get("type")
    if type_ not in DocType.ALL:
        type_ = None
    status = request.query_params.get("status")
    if status not in DocStatus.ALL:
        status = None

    items = documents_service.list_documents(
        db, ctx.group.id, type_=type_, status=status
    )
    if ctx.is_staff:
        counts = documents_service.counts_by_type(db, ctx.group.id)
    else:
        # Студент видит только общие распоряжения и свои документы — как и
        # на странице документа. Раньше список показывал чужие справки.
        items = [
            document
            for document in items
            if documents_service.can_view(document, ctx.membership)
        ]
        counts = {type_: 0 for type_ in DocType.ALL}
        for document in items:
            counts[document.type] = counts.get(document.type, 0) + 1
    return render(
        request,
        "documents.html",
        ctx,
        items=items,
        counts=counts,
        selected_type=type_,
        selected_status=status,
    )


@router.get("/documents/new")
def document_new(request: Request, db: DbSession, user: CurrentUser):
    ctx = build_context(request, db, user, page="documents")
    if ctx.group is None:
        return render(request, "onboarding.html", ctx)
    if not _ensure_staff(ctx):
        return _redirect(f"/documents?group={ctx.group.id}", err="Создавать документы может староста или заместитель")

    type_ = request.query_params.get("type", DocType.ORDER)
    if type_ not in DocType.ALL:
        type_ = DocType.ORDER
    students = attendance_service.roster(db, ctx.group.id)
    return render(
        request,
        "document_form.html",
        ctx,
        type_=type_,
        students=students,
        today=today_local(),
        default_status=documents_service.DEFAULT_STATUS.get(type_, DocStatus.DRAFT),
    )


@router.get("/documents/{doc_id}")
def document_detail(doc_id: int, request: Request, db: DbSession, user: CurrentUser):
    ctx = build_context(request, db, user, page="documents")
    if ctx.group is None:
        return render(request, "onboarding.html", ctx)

    document = db.get(Document, doc_id)
    if document is None or document.group_id != ctx.group.id:
        raise HTTPException(status_code=404, detail="Документ не найден")
    if not documents_service.can_view(document, ctx.membership):
        raise HTTPException(status_code=403, detail="Нет доступа к документу")

    allowed = [
        (status, DocStatus.LABELS.get(status, status))
        for status in sorted(documents_service.ALLOWED_TRANSITIONS.get(document.status, set()))
    ]
    student = db.get(Membership, document.student_id) if document.student_id else None
    students = list(document.students) or ([student] if student else [])
    excused_count = (document.meta or {}).get("excused_lessons")

    return render(
        request,
        "document_detail.html",
        ctx,
        document=document,
        student=student,
        students=students,
        allowed=allowed,
        excused_count=excused_count,
    )


@router.get("/files/{doc_id}")
def document_file(doc_id: int, request: Request, db: DbSession, user: CurrentUser):
    ctx = build_context(request, db, user, page="documents")
    if ctx.group is None:
        raise HTTPException(status_code=404)
    document = db.get(Document, doc_id)
    if document is None or document.group_id != ctx.group.id:
        raise HTTPException(status_code=404, detail="Документ не найден")
    if not documents_service.can_view(document, ctx.membership):
        raise HTTPException(status_code=403)
    path = documents_service.file_path(document)
    if path is None:
        raise HTTPException(status_code=404, detail="Файл не прикреплён")
    return FileResponse(path, filename=document.file_name or path.name)


@router.get("/stats")
def stats_page(request: Request, db: DbSession, user: CurrentUser):
    ctx = build_context(request, db, user, page="stats")
    if ctx.group is None:
        return render(request, "onboarding.html", ctx)

    today = today_local()
    date_from = parse_date(request.query_params.get("from"), today.replace(day=1))
    date_to = parse_date(request.query_params.get("to"), today)
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    stats = attendance_service.summary(db, ctx.group.id, date_from, date_to)
    totals = attendance_service.totals(stats)
    subjects = attendance_service.subject_summary(db, ctx.group.id, date_from, date_to)

    return render(
        request,
        "stats.html",
        ctx,
        stats=stats,
        totals=totals,
        subjects=subjects,
        date_from=date_from,
        date_to=date_to,
        today=today,
    )


@router.get("/stats.csv")
def stats_csv(request: Request, db: DbSession, user: CurrentUser):
    ctx = build_context(request, db, user, page="stats")
    if ctx.group is None:
        raise HTTPException(status_code=404)

    today = today_local()
    date_from = parse_date(request.query_params.get("from"), today.replace(day=1))
    date_to = parse_date(request.query_params.get("to"), today)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    stats = attendance_service.summary(db, ctx.group.id, date_from, date_to)
    subjects = attendance_service.subject_summary(db, ctx.group.id, date_from, date_to)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([f"Посещаемость группы {ctx.group.code}"])
    writer.writerow([f"Период: {format_period(date_from, date_to)}"])
    writer.writerow([])
    writer.writerow(
        ["Студент", "Был", "Не был", "Уважительная", "Посещаемость, %"]
    )
    for item in stats:
        writer.writerow(
            [
                item.member.full_name,
                item.present,
                item.absent,
                item.excused,
                item.percent if item.percent is not None else "",
            ]
        )
    writer.writerow([])
    writer.writerow(["Предмет", "Всего отметок", "Посещено", "Пропущено", "Уважительных", "%"])
    for subject in subjects:
        writer.writerow(
            [
                subject["name"],
                subject["total"],
                subject["attended"],
                subject["absent"],
                subject["excused"],
                subject["percent"] if subject["percent"] is not None else "",
            ]
        )

    content = "\ufeff" + buffer.getvalue()
    filename = f"attendance_{date_from.isoformat()}_{date_to.isoformat()}.csv"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reports")
def reports_page(request: Request, db: DbSession, user: CurrentUser):
    ctx = build_context(request, db, user, page="reports")
    if ctx.group is None:
        return render(request, "onboarding.html", ctx)
    if not _ensure_staff(ctx):
        return _redirect(
            f"/?group={ctx.group.id}",
            err="Ведомости доступны старосте и заместителю",
        )

    today = today_local()
    year, month = _parse_month(request.query_params.get("month"), today)
    report = vedomosti_service.propuski_report(db, ctx.group, year, month)
    paid = vedomosti_service.paid_report(db, ctx.group, year, month)

    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    return render(
        request,
        "reports.html",
        ctx,
        report=report,
        paid=paid,
        month_value=_month_value(year, month),
        prev_month=_month_value(prev_year, prev_month),
        next_month=_month_value(next_year, next_month),
        today=today,
        # Ссылки на файлы: Mini App открывает их в системном браузере, где
        # cookie нет, поэтому права подтверждает подпись в самой ссылке.
        propuski_token=sign_file_token(user_id=user.id, group_id=ctx.group.id),
        paid_token=sign_file_token(user_id=user.id, group_id=ctx.group.id),
    )


def _download_context(
    request: Request,
    db: DbSession,
    user: User | None,
    token: str | None,
    *,
    group_id: int | None = None,
) -> WebContext:
    if user is None:
        payload = verify_file_token(token)
        if payload is None:
            raise HTTPException(
                status_code=401,
                detail="Ссылка на файл устарела — откройте страницу отчётов заново",
            )
        user_id, token_group_id = payload
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="Ссылка на файл устарела")
        if group_id is not None and token_group_id != group_id:
            raise HTTPException(status_code=403, detail="Нет доступа к этой группе")
        ctx = build_context(
            request, db, user, page="reports", group_id=token_group_id
        )
    else:
        ctx = build_context(request, db, user, page="reports", group_id=group_id)

    if ctx.group is None:
        raise HTTPException(status_code=404)
    if not _ensure_staff(ctx):
        raise HTTPException(
            status_code=403, detail="Ведомости доступны старосте и заместителю"
        )
    return ctx


@router.get("/reports/propuski.docx")
def report_propuski(
    request: Request, db: DbSession, user: OptionalUser, token: str | None = None
):
    ctx = _download_context(request, db, user, token)
    year, month = _parse_month(request.query_params.get("month"), today_local())
    report = vedomosti_service.propuski_report(db, ctx.group, year, month)
    data = vedomosti_service.render_propuski(report)
    filename = (
        f"Ведомость пропусков {report.month_name} {year} {ctx.group.code}.docx"
    )
    return _docx_response(data, filename)


@router.get("/reports/paid.docx")
def report_paid(
    request: Request, db: DbSession, user: OptionalUser, token: str | None = None
):
    ctx = _download_context(request, db, user, token)
    year, month = _parse_month(request.query_params.get("month"), today_local())
    report = vedomosti_service.paid_report(db, ctx.group, year, month)
    data = vedomosti_service.render_paid(report)
    filename = (
        "Ведомость платных отработок "
        f"{report.month_name} {year} {ctx.group.code}.docx"
    )
    return _docx_response(data, filename)


@router.get("/groups/{group_id}/database.db")
def group_database(
    group_id: int,
    request: Request,
    db: DbSession,
    user: OptionalUser,
    token: str | None = None,
):
    ctx = _download_context(request, db, user, token, group_id=group_id)
    if ctx.group.id != group_id:
        raise HTTPException(status_code=404)
    path = group_db_service.export_group(db, ctx.group)
    return FileResponse(
        path,
        media_type="application/vnd.sqlite3",
        filename=f"starosta-{ctx.group.code}.db",
    )


@router.get("/help")
def help_page(request: Request, db: DbSession, user: CurrentUser):
    ctx = build_context(request, db, user, page="help")
    return render(request, "help.html", ctx)


@router.post("/auth/telegram", include_in_schema=False)
async def auth_telegram(request: Request, db: DbSession):
    """Обменивает подписанный initData на cookie-сессию для обычных переходов."""
    secret = session_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="Не задан BOT_TOKEN или SESSION_SECRET для проверки Telegram",
        )
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - пустое/битое тело считаем отсутствием initData
        payload = {}
    init_data = str(payload.get("init_data") or "") if isinstance(payload, dict) else ""
    data = validate_init_data(init_data)
    if data is None:
        raise HTTPException(status_code=401, detail="Неверная или устаревшая подпись Telegram")
    telegram_id, full_name, username = user_from_init_data(data)
    if telegram_id is None:
        raise HTTPException(status_code=401, detail="Telegram не передал пользователя")

    user = groups_service.ensure_user(
        db,
        telegram_id=telegram_id,
        full_name=full_name or "Пользователь",
        username=username,
    )
    db.commit()

    secure = not settings.dev_mode
    response = JSONResponse({"ok": True, "user_id": user.id})
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(user.id, secret=secret),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    response.set_cookie(
        MARKER_COOKIE,
        "1",
        max_age=SESSION_TTL,
        samesite="lax",
        secure=secure,
        path="/",
    )
    return response


# --------------------------------------------------------------------------- #
# Действия из веб-интерфейса
# --------------------------------------------------------------------------- #
@router.post("/actions/groups/create")
async def action_create_group(request: Request, db: DbSession, user: CurrentUser):
    form = await request.form()
    code = str(form.get("code") or "")
    name = str(form.get("name") or "") or None
    try:
        group, _membership = groups_service.create_group(
            db, user=user, code=code, name=name
        )
    except groups_service.GroupError as exc:
        return _redirect("/", err=str(exc))
    return _redirect("/", msg=f"Группа {group.code} создана. Осталось загрузить расписание.")


@router.post("/actions/groups/join")
async def action_join_group(request: Request, db: DbSession, user: CurrentUser):
    form = await request.form()
    try:
        group, _membership = groups_service.join_by_invite(
            db, user=user, invite_code=str(form.get("invite_code") or "")
        )
    except groups_service.GroupError as exc:
        return _redirect("/", err=str(exc))
    return _redirect("/", msg=f"Вы присоединились к группе {group.code}")


@router.post("/actions/demo")
def action_demo(request: Request, db: DbSession, user: CurrentUser):
    if not settings.dev_mode:
        raise HTTPException(status_code=403, detail="Демо-режим выключен")
    group = demo_service.seed_demo(db, user=user)
    return _redirect("/", msg=f"Демо-группа {group.code} готова: расписание, студенты и документы уже внутри.")


@router.post("/actions/sync")
async def action_sync(request: Request, db: DbSession, user: CurrentUser):
    form = await request.form()
    ctx = build_context(
        request, db, user, page="schedule", group_id=_form_group_id(form)
    )
    if ctx.group is None:
        return _redirect("/", err="Сначала создайте группу")
    if not _ensure_staff(ctx):
        return _redirect(_form_next(form, "/schedule"), err="Синхронизация доступна старосте и заместителю")

    term = parse_int(form.get("term"), ctx.term) or ctx.term
    if term not in (1, 2):
        term = ctx.term
    try:
        result = schedule_sync.sync_group(db, ctx.group, term, actor=user)
    except Exception as exc:  # noqa: BLE001 - показываем понятную ошибку в UI
        return _redirect(_form_next(form, "/schedule"), err=f"Ошибка синхронизации: {exc}")
    if result.status == "error":
        return _redirect(_form_next(form, "/schedule"), err=result.message)
    return _redirect(_form_next(form, "/schedule"), msg=result.message)


@router.post("/actions/attendance/toggle")
async def action_attendance_toggle(request: Request, db: DbSession, user: CurrentUser):
    form = await request.form()
    occurrence = db.get(LessonOccurrence, parse_int(form.get("occurrence_id"), 0) or 0)
    member = db.get(Membership, parse_int(form.get("member_id"), 0) or 0)
    ctx = build_context(
        request,
        db,
        user,
        page="journal",
        group_id=occurrence.group_id if occurrence else _form_group_id(form),
    )
    if ctx.group is None:
        return _redirect("/", err="Сначала создайте группу")
    if not _ensure_staff(ctx):
        return _redirect(_form_next(form, "/journal"), err="Отмечать посещаемость может староста или заместитель")
    if occurrence is None or occurrence.group_id != ctx.group.id:
        return _redirect(_form_next(form, "/journal"), err="Пара не найдена")
    if member is None or member.group_id != ctx.group.id:
        return _redirect(_form_next(form, "/journal"), err="Студент не найден")

    status = attendance_service.toggle(
        db, occurrence=occurrence, member=member, actor=user
    )
    message = (
        f"Пропуск: {member.full_name}"
        if status == AttendanceStatus.ABSENT
        else f"Снова присутствует: {member.full_name}"
    )
    return _redirect(
        _form_next(form, f"/journal?group={ctx.group.id}&date={occurrence.date}"),
        msg=message,
    )


@router.post("/actions/attendance/reset-day")
async def action_attendance_reset(request: Request, db: DbSession, user: CurrentUser):
    form = await request.form()
    ctx = build_context(
        request, db, user, page="journal", group_id=_form_group_id(form)
    )
    if ctx.group is None:
        return _redirect("/", err="Сначала создайте группу")
    if not _ensure_staff(ctx):
        return _redirect(_form_next(form, "/journal"), err="Недостаточно прав")

    day = parse_date(str(form.get("date") or ""), today_local())
    if day is None:
        return _redirect(_form_next(form, "/journal"), err="Не указана дата")
    removed = attendance_service.reset_day(db, ctx.group.id, day, actor=user)
    return _redirect(
        _form_next(form, f"/journal?group={ctx.group.id}&date={day}"),
        msg=(
            f"Снято ручных отметок: {removed}. "
            "Уважительные по документам остались."
        ),
    )


@router.post("/actions/occurrences/{occurrence_id}/note")
async def action_occurrence_note(
    occurrence_id: int, request: Request, db: DbSession, user: CurrentUser
):
    form = await request.form()
    occurrence = db.get(LessonOccurrence, occurrence_id)
    ctx = build_context(
        request,
        db,
        user,
        page="schedule",
        group_id=occurrence.group_id if occurrence else _form_group_id(form),
    )
    if ctx.group is None:
        return _redirect("/", err="Сначала создайте группу")
    if not _ensure_staff(ctx):
        return _redirect(_form_next(form, "/schedule"), err="Недостаточно прав")
    if occurrence is None or occurrence.group_id != ctx.group.id:
        return _redirect(_form_next(form, "/schedule"), err="Пара не найдена")

    note = " ".join(str(form.get("note") or "").split())
    occurrence.note = note[:2000] or None
    audit.log(
        db,
        action="occurrence.note",
        group_id=ctx.group.id,
        actor_id=user.id,
        entity_type="occurrence",
        entity_id=occurrence.id,
        payload={"note": occurrence.note},
    )
    db.commit()
    return _redirect(
        _form_next(form, "/schedule"),
        msg="Заметка сохранена" if occurrence.note else "Заметка удалена",
    )


@router.post("/actions/students/add")
async def action_add_student(request: Request, db: DbSession, user: CurrentUser):
    form = await request.form()
    ctx = build_context(
        request, db, user, page="students", group_id=_form_group_id(form)
    )
    if ctx.group is None:
        return _redirect("/", err="Сначала создайте группу")
    if not _ensure_staff(ctx):
        return _redirect(_back("/students", ctx), err="Добавлять студентов может староста или заместитель")
    try:
        groups_service.add_member(
            db,
            group=ctx.group,
            full_name=str(form.get("full_name") or ""),
            role=str(form.get("role") or Role.STUDENT),
            funding=str(form.get("funding") or "budget"),
            actor=user,
        )
    except groups_service.GroupError as exc:
        return _redirect(_back("/students", ctx), err=str(exc))
    return _redirect(_back("/students", ctx), msg="Студент добавлен")


@router.post("/actions/students/{member_id}/update")
async def action_update_student(
    member_id: int, request: Request, db: DbSession, user: CurrentUser
):
    form = await request.form()
    membership = db.get(Membership, member_id)
    ctx = build_context(
        request,
        db,
        user,
        page="students",
        group_id=membership.group_id if membership else _form_group_id(form),
    )
    if ctx.group is None:
        return _redirect("/", err="Сначала создайте группу")
    if not _ensure_staff(ctx):
        return _redirect(_back("/students", ctx), err="Недостаточно прав")

    if membership is None or membership.group_id != ctx.group.id:
        return _redirect(_back("/students", ctx), err="Студент не найден")
    try:
        groups_service.update_member(
            db,
            membership=membership,
            actor=user,
            role=str(form.get("role") or "") or None,
            status=str(form.get("status") or "") or None,
            full_name=str(form.get("full_name") or "") or None,
            funding=str(form.get("funding") or "") or None,
        )
    except groups_service.GroupError as exc:
        return _redirect(_back("/students", ctx), err=str(exc))
    return _redirect(_back("/students", ctx), msg="Данные обновлены")


@router.post("/actions/documents/create")
async def action_create_document(request: Request, db: DbSession, user: CurrentUser):
    form = await request.form()
    ctx = build_context(
        request, db, user, page="documents", group_id=_form_group_id(form)
    )
    if ctx.group is None:
        return _redirect("/", err="Сначала создайте группу")
    if not _ensure_staff(ctx):
        return _redirect("/documents", err="Недостаточно прав")

    type_ = str(form.get("type") or DocType.ORDER)
    if type_ not in DocType.ALL:
        return _redirect(
            f"/documents?group={ctx.group.id}", err="Неизвестный тип документа"
        )
    back = f"/documents/new?group={ctx.group.id}&type={type_}"
    student_ids = [
        int(value)
        for value in form.getlist("student_ids")
        if str(value).isdigit()
    ]
    single_student = parse_int(form.get("student_id"))
    if single_student and single_student not in student_ids:
        student_ids.append(single_student)
    try:
        document = documents_service.create_document(
            db,
            group=ctx.group,
            author=user,
            type_=type_,
            title=str(form.get("title") or "") or None,
            number=str(form.get("number") or "") or None,
            student_id=single_student,
            student_ids=student_ids,
            issued_at=parse_date(str(form.get("issued_at") or ""), today_local()),
            valid_from=parse_date(str(form.get("valid_from") or "")),
            valid_to=parse_date(str(form.get("valid_to") or "")),
            valid_from_time=_parse_time(form.get("valid_from_time")),
            valid_to_time=_parse_time(form.get("valid_to_time")),
            body=str(form.get("body") or ""),
            reason=str(form.get("reason") or "") or None,
            status=str(form.get("status") or "") or None,
        )
    except documents_service.DocumentError as exc:
        return _redirect(back, err=str(exc))

    upload = form.get("file")
    if upload is not None and getattr(upload, "filename", ""):
        try:
            data = await upload.read()
            stored = documents_service.save_bytes(data, upload.filename)
            documents_service.attach_file(
                db,
                document=document,
                stored_name=stored,
                original_name=upload.filename,
                actor=user,
            )
        except documents_service.DocumentError as exc:
            return _redirect(
                f"/documents/{document.id}?group={ctx.group.id}",
                err=f"Документ создан, но файл не прикреплён: {exc}",
            )

    return _redirect(
        f"/documents/{document.id}?group={ctx.group.id}",
        msg=f"Документ {document.number or document.title} создан",
    )


@router.post("/actions/documents/{doc_id}/status")
async def action_document_status(
    doc_id: int, request: Request, db: DbSession, user: CurrentUser
):
    form = await request.form()
    document = db.get(Document, doc_id)
    ctx = build_context(
        request,
        db,
        user,
        page="documents",
        group_id=document.group_id if document else _form_group_id(form),
    )
    if ctx.group is None:
        return _redirect("/", err="Сначала создайте группу")
    if not _ensure_staff(ctx):
        return _redirect(f"/documents/{doc_id}", err="Недостаточно прав")

    if document is None or document.group_id != ctx.group.id:
        return _redirect("/documents", err="Документ не найден")

    status = str(form.get("status") or "")
    try:
        documents_service.set_status(
            db,
            document=document,
            status=status,
            actor=user,
            comment=str(form.get("comment") or "") or None,
        )
    except documents_service.DocumentError as exc:
        return _redirect(
            f"/documents/{doc_id}?group={ctx.group.id}", err=str(exc)
        )
    return _redirect(
        f"/documents/{doc_id}?group={ctx.group.id}",
        msg=f"Статус изменён на «{DocStatus.LABELS.get(status, status)}»",
    )


@router.post("/actions/documents/{doc_id}/file")
async def action_document_file(
    doc_id: int, request: Request, db: DbSession, user: CurrentUser
):
    form = await request.form()
    document = db.get(Document, doc_id)
    ctx = build_context(
        request,
        db,
        user,
        page="documents",
        group_id=document.group_id if document else _form_group_id(form),
    )
    if ctx.group is None:
        return _redirect("/", err="Сначала создайте группу")
    if not _ensure_staff(ctx):
        return _redirect(f"/documents/{doc_id}", err="Недостаточно прав")

    if document is None or document.group_id != ctx.group.id:
        return _redirect("/documents", err="Документ не найден")

    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        return _redirect(f"/documents/{doc_id}?group={ctx.group.id}", err="Выберите файл")
    try:
        stored = documents_service.save_bytes(await upload.read(), upload.filename)
        documents_service.attach_file(
            db,
            document=document,
            stored_name=stored,
            original_name=upload.filename,
            actor=user,
        )
    except documents_service.DocumentError as exc:
        return _redirect(f"/documents/{doc_id}?group={ctx.group.id}", err=str(exc))
    return _redirect(f"/documents/{doc_id}?group={ctx.group.id}", msg="Файл прикреплён")


@router.post("/actions/documents/{doc_id}/delete")
async def action_document_delete(
    doc_id: int, request: Request, db: DbSession, user: CurrentUser
):
    form = await request.form()
    document = db.get(Document, doc_id)
    ctx = build_context(
        request,
        db,
        user,
        page="documents",
        group_id=document.group_id if document else _form_group_id(form),
    )
    if ctx.group is None:
        return _redirect("/", err="Сначала создайте группу")
    if not _ensure_staff(ctx):
        return _redirect(f"/documents/{doc_id}", err="Недостаточно прав")

    if document is None or document.group_id != ctx.group.id:
        return _redirect("/documents", err="Документ не найден")

    documents_service.delete_document(db, document=document, actor=user)
    return _redirect(_back("/documents", ctx), msg="Документ удалён")
