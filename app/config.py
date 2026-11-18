from __future__ import annotations

import socket
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# Адреса, которые с телефона не открываются: их заменяем на IP в Wi-Fi.
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "0.0.0.0", "::1"}


def detect_lan_ip() -> str:
    """IP этого компьютера в домашней/учебной сети (без реального подключения)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"


class Settings(BaseSettings):
    app_name: str = "Староста"
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'starosta.db'}"
    bot_token: str = ""
    bot_username: str = ""
    web_base_url: str = "http://localhost:8000"

    # В dev-режиме пользователь определяется по заголовку X-Telegram-Id
    # (или DEV_TELEGRAM_ID). В продакшене — только по подписанному initData.
    dev_mode: bool = True
    dev_telegram_id: int = 100000001
    dev_user_name: str = "Староста"

    upload_dir: Path = BASE_DIR / "data" / "uploads"
    max_upload_mb: int = 10
    timezone: str = "Europe/Minsk"

    # Сетевой адрес веб-сервера. 0.0.0.0 разрешает вход с телефона
    # по локальному IP; 127.0.0.1 — только с этого компьютера.
    host: str = "0.0.0.0"
    port: int = 8000

    # Учебные часы в одной паре и стоимость платной отработки.
    academic_hours_per_lesson: int = 2
    paid_lesson_rate: float = 18.47

    # Секрет для подписи cookie-сессии. Если пуст, используется BOT_TOKEN.
    session_secret: str = ""

    schedule_sync_hours: int = 6
    scheduler_enabled: bool = True
    scheduler_startup_delay_seconds: int = 60
    # 0 — использовать schedule_sync_hours * 60
    scheduler_interval_minutes: int = 0
    polessu_crawl_delay_seconds: float = 10.0
    parser_user_agent: str = (
        "StarostaApp/0.1 (student project; schedule import; contact: starosta@example.com)"
    )

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def web_url(self) -> str:
        """Адрес приложения для телефона.

        Если в WEB_BASE_URL стоит localhost или 0.0.0.0, подставляется IP
        компьютера в Wi-Fi — такой адрес открывается с телефона в браузере.
        """
        parts = urlsplit(self.web_base_url.strip())
        host = (parts.hostname or "").strip().lower()
        if host not in LOCAL_HOSTS:
            return self.web_base_url.rstrip("/")
        port = parts.port or self.port
        # Локальный сервер отдаёт обычный HTTP: https://127.0.0.1 — это ошибка
        # в настройках, из-за неё Telegram и показывал ERR_SSL_PROTOCOL_ERROR.
        return urlunsplit(("http", f"{detect_lan_ip()}:{port}", "", "", ""))

    @property
    def web_app_enabled(self) -> bool:
        """Mini App в Telegram требует публичный HTTPS-адрес."""
        return self.web_url.startswith("https://")


settings = Settings()
settings.upload_dir.mkdir(parents=True, exist_ok=True)
(BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
