"""Кнопка меню Telegram — общая для бота и веб-процесса.

Кнопку меню хранит Telegram, а не приложение. Когда туннель меняет адрес,
её нужно обновить через Bot API; иначе пользователь нажимает «Приложение»
и попадает на мёртвый домен.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger(__name__)

MENU_TEXT = "Приложение"


def menu_button_payload() -> dict[str, Any] | None:
    """Параметр menu_button для setChatMenuButton.

    None — обновлять нечем (нет токена). Без публичного HTTPS возвращаем
    default: кнопка не должна вести на адрес прошлого запуска.
    """
    if not settings.bot_token:
        return None
    if settings.web_app_enabled:
        return {
            "type": "web_app",
            "text": MENU_TEXT,
            "web_app": {"url": settings.web_url},
        }
    return {"type": "default"}


async def apply_menu_button() -> bool:
    """Отправляет кнопку меню в Telegram. Ошибки сети не пробрасывает."""
    payload = menu_button_payload()
    if payload is None:
        return False
    endpoint = f"https://api.telegram.org/bot{settings.bot_token}/setChatMenuButton"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(endpoint, json={"menu_button": payload})
    except httpx.HTTPError as exc:
        log.warning("Кнопка меню не обновлена: %s", exc)
        return False
    if response.status_code >= 400:
        log.warning(
            "Telegram отклонил кнопку меню: %s %s",
            response.status_code,
            response.text[:200],
        )
        return False
    return True
