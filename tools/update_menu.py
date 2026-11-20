"""Обновляет кнопку меню Telegram по адресу из .env.

Запускается сторожем `tools/tunnel-watch.ps1` сразу после смены адреса
туннеля, чтобы кнопка «Приложение» не ждала перезапуска бота:

    python -m tools.update_menu
"""

from __future__ import annotations

import asyncio

from app.config import settings
from app.services.telegram_menu import apply_menu_button


def main() -> int:
    print(f"Адрес: {settings.web_url}")
    if not settings.bot_token:
        print("BOT_TOKEN не задан — обновлять нечего.")
        return 1
    ok = asyncio.run(apply_menu_button())
    print("Кнопка меню обновлена." if ok else "Не удалось обновить кнопку меню.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
