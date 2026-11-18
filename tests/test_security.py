from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from app.config import settings
from app.security import sign_session, verify_session

BOT_TOKEN = "123456:TEST-TOKEN"
SECRET = "test-session-secret"


def _init_data(token: str, user_id: int, first_name: str = "Анна") -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAH-test",
        "user": json.dumps(
            {"id": user_id, "first_name": first_name, "username": "anna"},
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    }
    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**pairs, "hash": signature})


def test_session_roundtrip_and_tamper():
    token = sign_session(42, secret=SECRET)
    assert verify_session(token, secret=SECRET) == 42
    assert verify_session(token, secret="other") is None
    assert verify_session(token + "0", secret=SECRET) is None
    assert verify_session("мусор", secret=SECRET) is None


def test_telegram_init_data_becomes_session_cookie(client, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "session_secret", SECRET)

    response = client.post(
        "/auth/telegram",
        json={"init_data": _init_data(BOT_TOKEN, 777001, "Анна")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True

    me = client.get("/api/me")
    assert me.status_code == 200, me.text
    assert me.json()["user"]["telegram_id"] == 777001
    assert me.json()["user"]["full_name"] == "Анна"


def test_telegram_init_data_rejects_bad_signature(client, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "session_secret", SECRET)

    broken = _init_data(BOT_TOKEN, 777002).replace("hash=", "hash=0")
    response = client.post("/auth/telegram", json={"init_data": broken})
    assert response.status_code == 401


def test_session_cookie_grants_access(client, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "session_secret", SECRET)

    ok = client.post(
        "/auth/telegram", json={"init_data": _init_data(BOT_TOKEN, 777003, "Иван")}
    )
    assert ok.status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/api/me").json()["user"]["telegram_id"] == 777003
