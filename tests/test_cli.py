from __future__ import annotations

from app import cli


def test_stop_command_is_available():
    args = cli.build_parser().parse_args(["stop"])
    assert args.func is cli.cmd_stop
    assert args.port is None


def test_serve_on_busy_port_reports_already_running(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_port_busy", lambda host, port: True)
    monkeypatch.setattr(cli, "_health_ok", lambda port: True)
    monkeypatch.setattr(cli, "local_ip", lambda: "192.168.0.10")

    class Args:
        host = "0.0.0.0"
        port = 8000
        reload = False

    assert cli.cmd_serve(Args()) == 0
    output = capsys.readouterr().out
    assert "уже запущен" in output
    assert "http://192.168.0.10:8000" in output
    assert "app.cli stop" in output
