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


def test_broken_base_url_falls_back_to_lan(monkeypatch):
    # Опечатка вместо адреса не должна превращаться в «ссылку» для телефона.
    monkeypatch.setattr("app.config.detect_lan_ip", lambda: "192.168.0.10")
    settings = _settings(web_base_url="Thank")

    assert settings.web_url == "http://192.168.0.10:8000"
    assert settings.web_app_enabled is False


def test_port_comes_from_settings_when_url_has_none(monkeypatch):
    monkeypatch.setattr("app.config.detect_lan_ip", lambda: "10.0.0.5")
    settings = _settings(web_base_url="http://localhost", port=9000)

    assert settings.web_url == "http://10.0.0.5:9000"


def test_read_env_value_handles_quotes_and_comments(tmp_path):
    from app import config

    path = tmp_path / ".env"
    path.write_text(
        "# комментарий\nBOT_TOKEN=123\nWEB_BASE_URL='https://abc.app.lhr.life'\n",
        encoding="utf-8",
    )

    assert config.read_env_value("WEB_BASE_URL", path) == "https://abc.app.lhr.life"
    assert config.read_env_value("MISSING", path) is None


def test_refresh_web_base_url_picks_up_new_tunnel(monkeypatch, tmp_path):
    from app import config

    monkeypatch.setattr(config.settings, "web_base_url", "https://old.app.lhr.life")
    path = tmp_path / ".env"
    path.write_text("WEB_BASE_URL=https://old.app.lhr.life\n", encoding="utf-8")

    # Тот же адрес — ничего не меняем и не трогаем Telegram.
    assert config.refresh_web_base_url(path) is False
    assert config.settings.web_base_url == "https://old.app.lhr.life"

    # localhost.run выдал новый домен — процесс должен увидеть его без рестарта.
    path.write_text("WEB_BASE_URL=https://new.app.lhr.life\n", encoding="utf-8")
    assert config.refresh_web_base_url(path) is True
    assert config.settings.web_base_url == "https://new.app.lhr.life"
