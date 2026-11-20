from __future__ import annotations

from app.config import settings
from app.services.telegram_menu import menu_button_payload


def test_menu_button_uses_public_tunnel(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123:abc")
    monkeypatch.setattr(settings, "web_base_url", "https://abc.app.lhr.life")

    assert menu_button_payload() == {
        "type": "web_app",
        "text": "Приложение",
        "web_app": {"url": "https://abc.app.lhr.life"},
    }


def test_menu_button_resets_without_https(monkeypatch):
    # Без публичного HTTPS кнопка не должна вести на мёртвый адрес.
    monkeypatch.setattr(settings, "bot_token", "123:abc")
    monkeypatch.setattr(settings, "web_base_url", "http://localhost:8000")

    assert menu_button_payload() == {"type": "default"}


def test_menu_button_skipped_without_token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "")

    assert menu_button_payload() is None
