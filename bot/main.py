"""Телеграм-бот «Староста».

Запуск:
    python -m bot.main

Бот работает отдельно от веб-приложения и использует ту же базу данных.
"""

from __future__ import annotations

import asyncio
import html
from contextlib import suppress
from datetime import timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from sqlalchemy import func, select

from app.config import settings
from app.constants import AttendanceStatus, DocStatus, DocType, OccurrenceStatus, Role
from app.db import SessionLocal, init_db
from app.models import Attendance, Group, Membership, User
from app.services import attendance as attendance_service
from app.services import documents as documents_service
from app.services import schedule as schedule_service
from app.services import schedule_sync
from app.services.groups import GroupError, ensure_user, join_by_invite, membership_for_telegram
from app.services.notify import notify_staff_join
from app.services.telegram_menu import apply_menu_button
from app.services.tunnel import watch_base_url
from app.utils import today_local

router = Router()

INVITE_ALPHABET = set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")


def _membership_info(telegram_id: int) -> dict | None:
    with SessionLocal() as db:
        membership = membership_for_telegram(db, telegram_id)
        if membership is None:
            return None
        return {
            "membership_id": membership.id,
            "group_id": membership.group_id,
            "group_code": membership.group.code,
            "group_name": membership.group.name,
            "role": membership.role,
            "invite_code": membership.group.invite_code,
            "full_name": membership.full_name,
        }


def _no_group_text() -> str:
    text = (
        "Вы ещё не в группе.\n\n"
        "Откройте приложение и создайте группу или присоединитесь по коду.\n"
        "Можно также отправить боту код приглашения одним сообщением."
    )
    if settings.bot_username:
        text += f"\n\n@{settings.bot_username}"
    return text


def _app_keyboard() -> InlineKeyboardMarkup | None:
    url = settings.web_url
    if settings.web_app_enabled:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть приложение",
                        web_app=WebAppInfo(url=url),
                    )
                ]
            ]
        )
    # Mini App требует HTTPS. Без него даём обычную ссылку — она откроется
    # в браузере телефона (нужна та же Wi-Fi сеть).
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть в браузере", url=url)]]
    )


def _app_hint() -> str:
    if settings.web_app_enabled:
        return ""
    return (
        f"\n\nПриложение: {settings.web_url}\n"
        "Открывается в браузере телефона — телефон должен быть в той же Wi-Fi. "
        "Режим Mini App внутри Telegram включается только на HTTPS-адресе "
        "(cloudflared/ngrok)."
    )


def _join_job(
    telegram_id: int, full_name: str, username: str | None, code: str
) -> tuple[bool, str, tuple[int, int, int] | None]:
    with SessionLocal() as db:
        user = ensure_user(
            db, telegram_id=telegram_id, full_name=full_name, username=username
        )
        db.commit()
        try:
            group, membership = join_by_invite(db, user=user, invite_code=code)
        except GroupError as exc:
            return False, str(exc), None
        pending = (
            (group.id, membership.id, user.id) if membership.needs_review else None
        )
        return True, f"Вы присоединились к группе <b>{html.escape(group.code)}</b>.", pending


async def _notify_staff_about_join(pending: tuple[int, int, int] | None) -> None:
    """Сообщает старосте о заявке, не задерживая ответ студенту."""
    if pending is None:
        return
    group_id, membership_id, user_id = pending
    with SessionLocal() as db:
        group = db.get(Group, group_id)
        membership = db.get(Membership, membership_id)
        user = db.get(User, user_id)
        if group is not None and membership is not None:
            await notify_staff_join(db, group, membership, user)


def _day_text(group_id: int, day, title: str) -> str:
    with SessionLocal() as db:
        group = db.get(Group, group_id)
        term = schedule_service.terms_for_date(db, group, day)
        occurrences = [
            o
            for o in schedule_service.list_day(db, group_id, day)
            if o.lesson.term == term
            and o.status != OccurrenceStatus.CANCELLED
        ]
        if not occurrences:
            return f"<b>{html.escape(title)}</b>\n{day:%d.%m.%Y} — пар нет"

        lines = [
            f"<b>{html.escape(title)}</b>",
            f"{day:%d.%m.%Y}",
            "",
        ]
        for occurrence in occurrences:
            lesson = occurrence.lesson
            subject = html.escape(lesson.subject.name)
            room = html.escape(lesson.room) if lesson.room else "—"
            kind = html.escape(lesson.kind) if lesson.kind else ""
            subgroup = f" · {html.escape(lesson.subgroup)}" if lesson.subgroup else ""
            lines.append(
                f"<b>{lesson.start_time:%H:%M}–{lesson.end_time:%H:%M}</b> "
                f"{subject}\n     {kind + ' · ' if kind else ''}ауд. {room}{subgroup}"
            )
        return "\n".join(lines)


def _week_text(group_id: int) -> str:
    today = today_local()
    start = today - timedelta(days=today.weekday())
    with SessionLocal() as db:
        group = db.get(Group, group_id)
        term = schedule_service.terms_for_date(db, group, today)
        weeks = schedule_service.list_weeks(db, group_id, term)
        week = schedule_service.week_for_date(weeks, today)
        lines = ["<b>Неделя</b>"]
        if week:
            lines.append(
                f"Неделя {week['number']} · {start:%d.%m}–{(start + timedelta(days=6)):%d.%m}"
            )
        lines.append("")
        found = False
        for offset in range(7):
            day = start + timedelta(days=offset)
            occurrences = [
                o
                for o in schedule_service.list_day(db, group_id, day)
                if o.lesson.term == term
                and o.status != OccurrenceStatus.CANCELLED
            ]
            if not occurrences:
                continue
            found = True
            lines.append(f"<b>{day:%d.%m}</b>")
            for occurrence in occurrences:
                lesson = occurrence.lesson
                lines.append(
                    f"• {lesson.start_time:%H:%M} {html.escape(lesson.subject.name)}"
                    + (f" · {html.escape(lesson.room)}" if lesson.room else "")
                )
            lines.append("")
        if not found:
            lines.append("На этой неделе пар нет.")
        return "\n".join(lines).strip()


def _journal_text(group_id: int, day) -> str:
    with SessionLocal() as db:
        group = db.get(Group, group_id)
        term = schedule_service.terms_for_date(db, group, day)
        occurrences = [
            o
            for o in schedule_service.list_day(db, group_id, day)
            if o.lesson.term == term
            and o.status != OccurrenceStatus.CANCELLED
        ]
        if not occurrences:
            return f"<b>Журнал</b>\n{day:%d.%m.%Y} — пар нет"
        ids = [o.id for o in occurrences]
        rows = db.execute(
            select(Attendance.occurrence_id, Attendance.status, func.count(Attendance.id))
            .where(Attendance.occurrence_id.in_(ids))
            .group_by(Attendance.occurrence_id, Attendance.status)
        ).all()
        counts: dict[int, dict[str, int]] = {}
        for occurrence_id, status, count in rows:
            counts.setdefault(occurrence_id, {})[status] = count

        lines = ["<b>Журнал</b>", f"{day:%d.%m.%Y}", ""]
        total_members = len(attendance_service.roster(db, group_id))
        for occurrence in occurrences:
            data = counts.get(occurrence.id, {})
            absent = data.get(AttendanceStatus.ABSENT, 0)
            excused = data.get(AttendanceStatus.EXCUSED, 0)
            present = max(0, total_members - absent - excused)
            lines.append(
                f"<b>{occurrence.lesson.start_time:%H:%M}</b> "
                f"{html.escape(occurrence.lesson.subject.name)}\n"
                f"     был {present} · нет {absent} · уваж. {excused}"
            )
        return "\n".join(lines)


def _stats_text(group_id: int) -> str:
    today = today_local()
    with SessionLocal() as db:
        stats = attendance_service.summary(db, group_id, today.replace(day=1), today)
        totals = attendance_service.totals(stats)
    percent = totals["percent"]
    lines = [
        "<b>Посещаемость за месяц</b>",
        (
            f"Итого: {percent}% · был {totals['attended']} · "
            f"пропуск {totals['absent']} · уваж. {totals['excused']}"
        ),
    ]
    low = [item for item in stats if item.percent is not None and item.percent < 75]
    if low:
        lines.append("")
        lines.append("<b>Ниже 75%</b>")
        for item in low[:10]:
            lines.append(
                f"• {html.escape(item.member.full_name)} — {item.percent}% "
                f"(пропуск {item.absent})"
            )
    return "\n".join(lines)


def _documents_text(group_id: int, telegram_id: int) -> str:
    with SessionLocal() as db:
        membership = membership_for_telegram(db, telegram_id, group_id)
        if membership is None:
            return "Группа не найдена."
        items = documents_service.list_documents(db, group_id, limit=30)
        visible = [
            document
            for document in items
            if documents_service.can_view(document, membership)
        ][:5]
        if not visible:
            return "<b>Документы</b>\nПока ничего нет."
        lines = ["<b>Последние документы</b>"]
        for document in visible:
            date = document.issued_at.strftime("%d.%m") if document.issued_at else "—"
            lines.append(
                f"• {date} · {DocType.LABELS.get(document.type, document.type)}: "
                f"{html.escape(document.title)}\n"
                f"     {DocStatus.LABELS.get(document.status, document.status)}"
            )
        if settings.web_app_enabled:
            lines.append("")
            lines.append("Полный список — в приложении.")
        return "\n".join(lines)


def _sync_job(group_id: int, term: int, telegram_id: int) -> str:
    with SessionLocal() as db:
        group = db.get(Group, group_id)
        membership = membership_for_telegram(db, telegram_id, group_id)
        if group is None or membership is None:
            return "Группа не найдена."
        if membership.role not in Role.STAFF:
            return "Синхронизация доступна старосте и заместителю."
        result = schedule_sync.sync_group(db, group, term)
        if result.status == "error":
            return f"Не удалось обновить расписание:\n{html.escape(result.message)}"
        if result.status == "unchanged":
            return "Расписание не изменилось."
        return (
            "Расписание обновлено.\n"
            f"Пар в семестре: {result.lessons} · недель: {result.weeks}\n"
            f"Новых занятий: {result.occurrences_created} · "
            f"отменено: {result.occurrences_cancelled}"
        )


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    joined = ""
    pending = None
    code = (command.args or "").strip()
    if code:
        _ok, text, pending = await asyncio.to_thread(
            _join_job,
            message.from_user.id,
            message.from_user.full_name,
            message.from_user.username,
            code,
        )
        joined = text + "\n\n"

    info = _membership_info(message.from_user.id)
    if info:
        header = (
            f"Группа <b>{html.escape(info['group_code'])}</b>, "
            f"роль: {Role.LABELS.get(info['role'], info['role'])}."
        )
    else:
        header = "Вы пока не в группе."

    text = (
        f"<b>Староста</b> — неофициальный помощник.\n\n"
        f"{joined}{header}\n\n"
        "Команды:\n"
        "/app — открыть приложение\n"
        "/today — пары сегодня\n"
        "/tomorrow — пары завтра\n"
        "/week — расписание на неделю\n"
        "/journal — сводка посещаемости за сегодня\n"
        "/stats — посещаемость за месяц\n"
        "/documents — последние документы\n"
        "/invite — код и ссылка-приглашение\n"
        "/sync — обновить расписание с сайта вуза\n"
        "/help — помощь"
    ) + _app_hint()
    await message.answer(text, reply_markup=_app_keyboard())
    # Сообщение старосте — после ответа студенту: сеть Telegram не должна
    # задерживать /start.
    await _notify_staff_about_join(pending)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Как пользоваться</b>\n\n"
        "1. Откройте приложение и создайте группу или присоединитесь по коду.\n"
        "2. Староста загружает расписание с polessu.by.\n"
        "3. Посещаемость отмечается в журнале приложения.\n"
        "4. Распоряжения и справки автоматически ставят «уважительную».\n\n"
        "Код приглашения можно отправить боту одним сообщением — "
        "он присоединит вас к группе.\n\n"
        "Данные неофициальные: при расхождении приоритет у вуза."
        + _app_hint(),
        reply_markup=_app_keyboard(),
    )


@router.message(Command("app"))
async def cmd_app(message: Message) -> None:
    keyboard = _app_keyboard()
    if settings.web_app_enabled:
        await message.answer(
            f"Mini App активен: <code>{settings.web_url}</code>",
            reply_markup=keyboard,
        )
    else:
        await message.answer(
            "<b>Mini App выключен.</b>\n\n"
            f"Текущий адрес: <code>{settings.web_url}</code>\n\n"
            "Telegram открывает WebApp только по HTTPS. Чтобы включить:\n"
            "1. Закрой бота и запусти <code>start.bat</code> — он поднимет туннель\n"
            "   и сам впишет адрес в <code>.env</code>\n"
            "2. Перезапусти бота — кнопку меню он обновит сам",
            reply_markup=keyboard,
        )


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    info = _membership_info(message.from_user.id)
    if not info:
        await message.answer(_no_group_text())
        return
    await message.answer(_day_text(info["group_id"], today_local(), "Сегодня"))


@router.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message) -> None:
    info = _membership_info(message.from_user.id)
    if not info:
        await message.answer(_no_group_text())
        return
    await message.answer(
        _day_text(info["group_id"], today_local() + timedelta(days=1), "Завтра")
    )


@router.message(Command("week"))
async def cmd_week(message: Message) -> None:
    info = _membership_info(message.from_user.id)
    if not info:
        await message.answer(_no_group_text())
        return
    await message.answer(_week_text(info["group_id"]))


@router.message(Command("journal"))
async def cmd_journal(message: Message) -> None:
    info = _membership_info(message.from_user.id)
    if not info:
        await message.answer(_no_group_text())
        return
    await message.answer(_journal_text(info["group_id"], today_local()))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    info = _membership_info(message.from_user.id)
    if not info:
        await message.answer(_no_group_text())
        return
    text = await asyncio.to_thread(_stats_text, info["group_id"])
    await message.answer(text)


@router.message(Command("documents"))
async def cmd_documents(message: Message) -> None:
    info = _membership_info(message.from_user.id)
    if not info:
        await message.answer(_no_group_text())
        return
    text = await asyncio.to_thread(
        _documents_text, info["group_id"], message.from_user.id
    )
    await message.answer(text, reply_markup=_app_keyboard())


@router.message(Command("invite"))
async def cmd_invite(message: Message) -> None:
    info = _membership_info(message.from_user.id)
    if not info:
        await message.answer(_no_group_text())
        return
    if info["role"] not in Role.STAFF:
        await message.answer("Код приглашения может посмотреть староста или заместитель.")
        return

    lines = [
        f"Код приглашения в группу <b>{html.escape(info['group_code'])}</b>:",
        f"<code>{info['invite_code']}</code>",
    ]
    if settings.bot_username:
        link = f"https://t.me/{settings.bot_username}?start={info['invite_code']}"
        lines.extend(["", f"Ссылка: {html.escape(link)}"])
    else:
        lines.extend(
            ["", "Ссылка появится после настройки BOT_USERNAME в .env."]
        )
    await message.answer("\n".join(lines))


@router.message(Command("sync"))
async def cmd_sync(message: Message) -> None:
    info = _membership_info(message.from_user.id)
    if not info:
        await message.answer(_no_group_text())
        return
    with SessionLocal() as db:
        group = db.get(Group, info["group_id"])
        term = (
            schedule_service.terms_for_date(db, group, today_local())
            if group
            else 1
        )
    await message.answer("Обновляю расписание…")
    text = await asyncio.to_thread(
        _sync_job, info["group_id"], term, message.from_user.id
    )
    await message.answer(text)


@router.message(F.text)
async def fallback(message: Message) -> None:
    text = (message.text or "").strip().upper()
    info = _membership_info(message.from_user.id)
    if (
        info is None
        and 4 <= len(text) <= 12
        and set(text) <= INVITE_ALPHABET
    ):
        ok, answer, pending = await asyncio.to_thread(
            _join_job,
            message.from_user.id,
            message.from_user.full_name,
            message.from_user.username,
            text,
        )
        if ok:
            await message.answer(answer, reply_markup=_app_keyboard())
            await _notify_staff_about_join(pending)
        else:
            await message.answer(answer)
        return
    await message.answer(
        "Не понял команду. Список: /help\n"
        "Или откройте приложение кнопкой из /start."
    )


async def main() -> None:
    if not settings.bot_token:
        print(
            "BOT_TOKEN не задан.\n"
            "Вставьте токен от @BotFather в файл starosta-app/.env:\n"
            "    BOT_TOKEN=123456789:AA...\n"
            "Затем запустите снова: python -m bot.main"
        )
        return

    init_db()
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Начало и приглашение"),
                BotCommand(command="app", description="Открыть приложение"),
                BotCommand(command="today", description="Пары сегодня"),
                BotCommand(command="tomorrow", description="Пары завтра"),
                BotCommand(command="week", description="Расписание на неделю"),
                BotCommand(command="journal", description="Посещаемость за сегодня"),
                BotCommand(command="stats", description="Посещаемость за месяц"),
                BotCommand(command="documents", description="Последние документы"),
                BotCommand(command="invite", description="Код приглашения"),
                BotCommand(command="sync", description="Обновить расписание"),
                BotCommand(command="help", description="Помощь"),
            ]
        )
        await bot.set_my_short_description(
            short_description="Расписание, журнал и документы учебной группы"
        )
        await bot.set_my_description(
            description=(
                "Неофициальный помощник старосты: расписание ПолесГУ, "
                "посещаемость, распоряжения, заявления и справки.\n\n"
                "Нажмите «Открыть приложение» или отправьте /start."
            )
        )
        # Кнопка меню слева от поля ввода. Обновляется при каждом запуске,
        # поэтому при смене адреса туннеля не нужно заходить в @BotFather.
        # Если туннель отключён, кнопка вернётся к стандартной и не будет
        # вести на мёртвый HTTPS-адрес прошлого запуска.
        await apply_menu_button()
    except TelegramAPIError as exc:
        print(f"Не удалось обновить профиль бота: {exc}")

    print("Бот запущен. Остановить: Ctrl+C")
    print(settings.mini_app_status)
    url_task = asyncio.create_task(watch_base_url(), name="tunnel-watch")
    try:
        await dispatcher.start_polling(bot)
    finally:
        url_task.cancel()
        with suppress(asyncio.CancelledError):
            await url_task
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
