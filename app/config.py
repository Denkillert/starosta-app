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
    # true — гостевой вход разрешён и через туннель (небезопасно: любой, кто
    # знает ссылку, попадёт в приложение под dev-пользователем).
    dev_mode_public: bool = False
    dev_telegram_id: int = 100000001
    dev_user_name: str = "Староста"

    upload_dir: Path = BASE_DIR / "data" / "uploads"
    max_upload_mb: int = 10
    timezone: str = "Europe/Minsk"

    # Сетевой адрес веб-сервера. 0.0.0.0 разрешает вход с телефона
    # по локальному IP; 127.0.0.1 — только с этого компьютера.
    host: str = "0.0.0.0"
    port: int = 8000

    # Учебные часы в одной паре и стоимость платной отработки за пару.
    academic_hours_per_lesson: int = 2
    paid_lesson_rate: float = 18.47

    # Секрет для подписи cookie-сессии. Если пуст, используется BOT_TOKEN.
    session_secret: str = ""

    # Сообщать старосте и заму в Telegram о новых заявках по коду приглашения.
    notify_staff_on_join: bool = True

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
        Если стоит публичный HTTPS-адрес (cloudflared/ngrok/домен) — он
        используется как есть и включает режим Mini App.
        """
        raw = self.web_base_url.strip()
        parts = urlsplit(raw)
        host = (parts.hostname or "").strip().lower()
        scheme = parts.scheme.lower()
        # Публичный адрес принимаем только как http(s) с настоящим хостом.
        # Иначе (пусто, опечатка, «localhost») работаем по IP в Wi-Fi.
        if scheme in {"http", "https"} and host and host not in LOCAL_HOSTS:
            return raw.rstrip("/")
        try:
            port = parts.port or self.port
        except ValueError:
            # Кривой порт в WEB_BASE_URL («http://host:abc») не должен ронять
            # приложение при импорте настроек.
            port = self.port
        # Локальный сервер отдаёт обычный HTTP: https://127.0.0.1 — это ошибка
        # в настройках, из-за неё Telegram показывал ERR_SSL_PROTOCOL_ERROR.
        return urlunsplit(("http", f"{detect_lan_ip()}:{port}", "", "", ""))

    @property
    def web_app_enabled(self) -> bool:
        """Mini App в Telegram требует публичный HTTPS-адрес."""
        return self.web_url.startswith("https://")

    @property
    def mini_app_status(self) -> str:
        """Человекочитаемый статус Mini App — для логов и подсказок в боте."""
        if self.web_app_enabled:
            return f"Mini App готов: {self.web_url}"
        return (
            f"Mini App НЕ включён. Текущий адрес: {self.web_url}. "
            "Telegram открывает WebApp только по HTTPS. "
            "Запусти cloudflared/ngrok и пропиши https://... в WEB_BASE_URL в .env, "
            "затем перезапусти бота и настрой /setmenubutton в @BotFather."
        )


settings = Settings()
settings.upload_dir.mkdir(parents=True, exist_ok=True)
(BASE_DIR / "data").mkdir(parents=True, exist_ok=True)


def read_env_value(name: str, path: Path | None = None) -> str | None:
    """Значение `KEY=...` прямо из .env.

    Нужен для WEB_BASE_URL: адрес туннеля меняется на ходу, и сторожа
    (`tools/tunnel-watch.ps1`) переписывают .env у уже запущенных процессов.
    """
    env_path = path or (BASE_DIR / ".env")
    try:
        text = env_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            value = value.strip().strip('"').strip("'")
            return value or None
    return None


def refresh_web_base_url(path: Path | None = None) -> bool:
    """Перечитывает WEB_BASE_URL из .env. True — адрес изменился.

    localhost.run на бесплатном тарифе регулярно выдаёт новый домен, при этом
    ssh.exe и веб-сервер продолжают работать. Без перечитывания .env бот и
    страницы остаются со ссылкой на уже удалённый туннель.
    """
    value = read_env_value("WEB_BASE_URL", path)
    if not value or value == settings.web_base_url:
        return False
    settings.web_base_url = value
    return True

# Явно пишем в консоль при импорте, чтобы было видно, в каком режиме стартует.
print(f"[config] {settings.mini_app_status}")
