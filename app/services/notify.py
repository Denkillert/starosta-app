"""Уведомления старосте и заместителю о новых заявках в группу.

Веб-процесс и бот работают в разных процессах, поэтому сообщения отправляются
через Bot API обычным HTTP-запросом. Любая ошибка сети/Telegram не должна
ломать вступление студента по коду приглашения.
"""

from __future__ import annotations

import logging
from html import escape

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import MemberStatus, Role
from ..models import Group, Membership, User

log = logging.getLogger(__name__)


def staff_telegram_ids(db: Session, group_id: int) -> list[int]:
    rows = db.scalars(
        select(User.telegram_id)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.group_id == group_id,
            Membership.status == MemberStatus.ACTIVE,
            Membership.role.in_(Role.STAFF),
            User.telegram_id.is_not(None),
        )
    ).all()
    return [int(value) for value in rows if value]


def _message_text(group: Group, membership: Membership, user: User | None) -> str:
    name = escape(membership.full_name or "Студент")
    username = getattr(user, "username", None)
    handle = f" (@{escape(username)})" if username else ""
    return (
        f"<b>Новая заявка в группу {escape(group.code)}</b>\n"
        f"Вступил(а) по коду приглашения: {name}{handle}.\n"
        "Проверьте ФИО, подгруппу и форму обучения: "
        "«Студенты» → «Заявки»."
    )


def _keyboard(group: Group) -> dict | None:
    if not settings.web_app_enabled:
        return None
    url = f"{settings.web_url}/students?group={group.id}"
    return {
        "inline_keyboard": [
            [{"text": "Открыть заявки", "web_app": {"url": url}}],
        ]
    }


async def notify_staff_join(
    db: Session,
    group: Group,
    membership: Membership,
    user: User | None = None,
) -> None:
    """Сообщает всем старостам и замам группы о новой заявке."""
    if not settings.notify_staff_on_join or not settings.bot_token:
        return
    try:
        recipients = staff_telegram_ids(db, group.id)
    except Exception:  # pragma: no cover - защита от неожиданной ошибки БД
        log.exception("Не удалось получить список старост группы %s", group.id)
        return
    if not recipients:
        return

    payload = {
        "text": _message_text(group, membership, user or membership.user),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    markup = _keyboard(group)
    if markup:
        payload["reply_markup"] = markup

    endpoint = f"https://api.telegram.org/bot{settings.bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=6) as client:
        for chat_id in recipients:
            try:
                response = await client.post(
                    endpoint, json={**payload, "chat_id": chat_id}
                )
                if response.status_code >= 400:
                    log.warning(
                        "Telegram не принял уведомление для %s: %s %s",
                        chat_id,
                        response.status_code,
                        response.text[:200],
                    )
            except Exception:  # pragma: no cover - сеть недоступна
                log.warning("Не удалось отправить уведомление в Telegram", exc_info=True)
