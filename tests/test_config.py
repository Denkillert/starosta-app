from __future__ import annotations

from app.config import Settings


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_localhost_base_url_becomes_lan_ip(monkeypatch):
    monkeypatch.setattr("app.config.detect_lan_ip", lambda: "192.168.0.10")
    settings = _settings(web_base_url="http://localhost:8000")

    assert settings.web_url == "http://192.168.0.10:8000"
    assert settings.web_app_enabled is False


def test_https_on_localhost_is_downgraded_to_http(monkeypatch):
    monkeypatch.setattr("app.config.detect_lan_ip", lambda: "192.168.0.10")
    settings = _settings(web_base_url="https://127.0.0.1:8000")

    # Иначе Telegram шёл по HTTPS на обычный HTTP-сервер и падал
    # с ERR_SSL_PROTOCOL_ERROR.
    assert settings.web_url == "http://192.168.0.10:8000"
    assert settings.web_app_enabled is False


def test_public_https_url_is_kept(monkeypatch):
    monkeypatch.setattr("app.config.detect_lan_ip", lambda: "192.168.0.10")
    settings = _settings(web_base_url="https://starosta.trycloudflare.com/")

    assert settings.web_url == "https://starosta.trycloudflare.com"
    assert settings.web_app_enabled is True


def test_port_comes_from_settings_when_url_has_none(monkeypatch):
    monkeypatch.setattr("app.config.detect_lan_ip", lambda: "10.0.0.5")
    settings = _settings(web_base_url="http://localhost", port=9000)

    assert settings.web_url == "http://10.0.0.5:9000"
