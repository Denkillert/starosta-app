"""Слежение за публичным адресом туннеля.

localhost.run на бесплатном тарифе периодически заменяет домен
(в логе видно `tunnel ... removed` и новый адрес). ssh.exe при этом
остаётся жив, поэтому без перечитывания .env бот и Mini App продолжают
показывать старую, уже удалённую ссылку.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import refresh_web_base_url, settings
from .telegram_menu import apply_menu_button

log = logging.getLogger(__name__)

CHECK_SECONDS = 20.0


async def watch_base_url(interval: float = CHECK_SECONDS) -> None:
    """Раз в interval секунд сверяет адрес в .env с адресом в памяти."""
    while True:
        try:
            if refresh_web_base_url():
                log.warning("Адрес туннеля изменился: %s", settings.web_url)
                await apply_menu_button()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - сторож не должен падать
            log.exception("Не удалось перечитать адрес туннеля")
        await asyncio.sleep(interval)
