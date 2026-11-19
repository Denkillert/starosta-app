from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from .config import settings

SESSION_COOKIE = "starosta_session"
MARKER_COOKIE = "starosta_auth"
SESSION_TTL = 30 * 24 * 3600


def session_secret() -> str:
    return settings.session_secret or settings.bot_token


def sign_session(user_id: int, *, secret: str, ttl: int = SESSION_TTL) -> str:
    expires = int(time.time()) + ttl
    payload = f"{user_id}.{expires}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_session(value: str | None, *, secret: str) -> int | None:
    if not value or not secret:
        return None
    try:
        raw_id, raw_expires, signature = value.split(".")
        user_id = int(raw_id)
        expires = int(raw_expires)
    except (TypeError, ValueError):
        return None
    payload = f"{user_id}.{expires}"
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    if expires < time.time():
        return None
    return user_id


FILE_TOKEN_TTL = 3600


def sign_file_token(
    *, user_id: int, group_id: int, secret: str | None = None, ttl: int = FILE_TOKEN_TTL
) -> str:
    """Ссылка на файл, которую можно открыть в браузере без cookie Telegram.

    Mini App скачивает файлы только через системный браузер, а там cookie
    веб-приложения уже нет — поэтому права подтверждаются подписью в ссылке.
    """
    secret = secret if secret is not None else session_secret()
    expires = int(time.time()) + ttl
    payload = f"file.{user_id}.{group_id}.{expires}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{user_id}.{group_id}.{expires}.{signature}"


def verify_file_token(
    token: str | None, *, secret: str | None = None
) -> tuple[int, int] | None:
    secret = secret if secret is not None else session_secret()
    if not token or not secret:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    try:
        user_id, group_id, expires = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if expires < time.time():
        return None
    payload = f"file.{user_id}.{group_id}.{expires}"
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts[3]):
        return None
    return user_id, group_id


def validate_init_data(init_data: str, bot_token: str | None = None, max_age: int = 86400) -> dict | None:
    """Проверяет подпись Telegram WebApp initData.

    Возвращает распарсенные данные либо None, если подпись неверна/устарела.
    """
    token = bot_token or settings.bot_token
    if not token or not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    # Без auth_date подпись можно было бы использовать вечно.
    if auth_date <= 0:
        return None
    now = time.time()
    if auth_date > now + 300:
        return None
    if max_age and now - auth_date > max_age:
        return None
    return pairs


def user_from_init_data(data: dict) -> tuple[int | None, str, str | None]:
    telegram_id = None
    full_name = ""
    username = None
    raw_user = data.get("user")
    if raw_user:
        try:
            payload = json.loads(raw_user)
        except json.JSONDecodeError:
            payload = {}
        try:
            telegram_id = int(payload.get("id")) if payload.get("id") else None
        except (TypeError, ValueError):
            telegram_id = None
        parts = [payload.get("first_name") or "", payload.get("last_name") or ""]
        full_name = " ".join(p.strip() for p in parts if p.strip())
        username = payload.get("username")
    return telegram_id, full_name, username
