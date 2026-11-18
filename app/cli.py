from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from sqlalchemy import select

from .config import detect_lan_ip, settings
from .db import SessionLocal, init_db
from .models import Group, LessonOccurrence, Membership
from .services import schedule as schedule_service
from .services import schedule_sync, scheduler
from .services.demo import seed_demo
from .services.groups import GroupError, create_group, ensure_user


def _dev_user(db):
    user = ensure_user(
        db,
        telegram_id=settings.dev_telegram_id,
        full_name=settings.dev_user_name,
    )
    db.commit()
    return user


def cmd_init_db(_args: argparse.Namespace) -> int:
    init_db()
    print(f"База готова: {settings.database_url}")
    return 0


def local_ip() -> str:
    """Локальный IP в домашней/учебной сети (без реального подключения)."""
    return detect_lan_ip()


def _health_ok(port: int) -> bool:
    """Проверяет, отвечает ли на порту именно это приложение."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=1.5
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _port_busy(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((probe_host, port)) == 0


def _report_busy(host: str, port: int) -> int:
    ip = local_ip()
    if _health_ok(port):
        print(f"Сервер уже запущен на порту {port}.", flush=True)
        print(f"  На этом компьютере:  http://127.0.0.1:{port}", flush=True)
        if host in ("0.0.0.0", "::"):
            print(f"  В локальной сети:    http://{ip}:{port}", flush=True)
        print(
            "  Второй экземпляр не нужен — просто откройте адрес в браузере.",
            flush=True,
        )
        print(
            "  Остановить работающий сервер: "
            r".\.venv\Scripts\python.exe -m app.cli stop",
            flush=True,
        )
        return 0

    print(f"Порт {port} занят другой программой.", file=sys.stderr)
    print(
        f"  Освободите порт или запустите на другом: "
        f"python -m app.cli serve --port {port + 1}",
        file=sys.stderr,
    )
    return 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    host = args.host or settings.host
    port = args.port or settings.port
    ip = local_ip()

    if _port_busy(host, port):
        return _report_busy(host, port)

    print("Староста — веб-сервер", flush=True)
    print(f"  На этом компьютере:  http://127.0.0.1:{port}", flush=True)
    if host in ("0.0.0.0", "::"):
        print(f"  В локальной сети:    http://{ip}:{port}", flush=True)
        print(
            "  С телефона откройте второй адрес (телефон в той же Wi-Fi).",
            flush=True,
        )
        print(
            "  Если не открывается — разрешите порт в брандмауэре Windows "
            f"(от имени администратора):\n"
            f'    New-NetFirewallRule -DisplayName "Starosta {port}" '
            f"-Direction Inbound -LocalPort {port} -Protocol TCP -Action Allow",
            flush=True,
        )
    print("  Остановить: Ctrl+C", flush=True)
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=args.reload,
    )
    return 0


def _listening_pids(port: int) -> list[int]:
    pids: set[int] = set()
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) < 5 or parts[0].upper() != "TCP":
                    continue
                if parts[3].upper() != "LISTENING":
                    continue
                if parts[1].rsplit(":", 1)[-1] != str(port):
                    continue
                if parts[4].isdigit():
                    pids.add(int(parts[4]))
        else:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            for value in result.stdout.split():
                if value.isdigit():
                    pids.add(int(value))
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(pids)


def _is_python_process(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return "python" in result.stdout.lower()
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return "python" in result.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_stop(args: argparse.Namespace) -> int:
    port = args.port or settings.port
    pids = _listening_pids(port)
    if not pids:
        print(f"На порту {port} никто не слушает — сервер уже остановлен.")
        return 0

    stopped: list[int] = []
    for pid in pids:
        if not _is_python_process(pid):
            print(
                f"PID {pid} на порту {port} — не Python-процесс, пропускаю.",
                file=sys.stderr,
            )
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
        except OSError as exc:
            print(f"Не удалось остановить PID {pid}: {exc}", file=sys.stderr)

    for _ in range(20):
        if not _port_busy("127.0.0.1", port):
            break
        time.sleep(0.25)

    if not stopped:
        print(f"Не удалось освободить порт {port}.", file=sys.stderr)
        return 1
    print(
        f"Остановлено: {', '.join(str(pid) for pid in stopped)}. "
        f"Порт {port} свободен."
    )
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    init_db()
    with SessionLocal() as db:
        user = _dev_user(db)
        group = seed_demo(
            db,
            user=user,
            term=args.term,
            with_attendance=not args.no_attendance,
        )
        membership = db.scalar(
            select(Membership).where(
                Membership.group_id == group.id, Membership.user_id == user.id
            )
        )
        occurrences = db.scalar(
            select(LessonOccurrence.id)
            .where(LessonOccurrence.group_id == group.id)
            .limit(1)
        )
        print(f"Группа: {group.code} (id={group.id})")
        print(f"Код приглашения: {group.invite_code}")
        print(f"Роль: {membership.role if membership else '—'}")
        print(f"Расписание: {'загружено' if occurrences else 'нет'}")

        if args.live:
            term = args.term
            result = schedule_sync.sync_group(db, group, term, actor=user, force=True)
            print(f"Живая синхронизация: {result.status} — {result.message}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    init_db()
    with SessionLocal() as db:
        group = db.scalar(select(Group).where(Group.code == args.group))
        if group is None:
            print(f"Группа {args.group!r} не найдена. Сначала выполните seed.", file=sys.stderr)
            return 1
        user = _dev_user(db)
        result = schedule_sync.sync_group(
            db, group, args.term, actor=user, force=args.force
        )
        print(f"{result.status}: {result.message}")
        if result.status == "ok":
            print(
                f"строк: {result.lessons}, недель: {result.weeks}, "
                f"создано пар: {result.occurrences_created}, "
                f"отменено: {result.occurrences_cancelled}"
            )
        return 0 if result.status != "error" else 1


def cmd_sync_all(args: argparse.Namespace) -> int:
    init_db()
    with SessionLocal() as db:
        outcomes = scheduler.sync_due_groups(
            db,
            hours=args.hours,
            force=args.force,
            terms=[args.term] if args.term else None,
            delay=args.delay,
        )
    if not outcomes:
        print("Все расписания свежие — обновлять нечего.")
        return 0
    for outcome in outcomes:
        print(
            f"{outcome.code} · семестр {outcome.term}: "
            f"{outcome.status} — {outcome.message}"
        )
    return 0 if all(o.status != "error" for o in outcomes) else 1


def cmd_create_group(args: argparse.Namespace) -> int:
    init_db()
    with SessionLocal() as db:
        user = _dev_user(db)
        try:
            group, _membership = create_group(
                db, user=user, code=args.code, name=args.name
            )
        except GroupError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Группа {group.code} создана. Код приглашения: {group.invite_code}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    init_db()
    with SessionLocal() as db:
        group = db.scalar(select(Group).where(Group.code == args.group))
        if group is None:
            print("Группа не найдена", file=sys.stderr)
            return 1
        members = db.scalars(
            select(Membership).where(Membership.group_id == group.id)
        ).all()
        print(f"{group.code} · {group.name} · id={group.id}")
        print(f"Код приглашения: {group.invite_code}")
        print(f"Участников: {len(members)}")
        for term in (1, 2):
            weeks = schedule_service.list_weeks(db, group.id, term)
            snapshot = schedule_service.latest_snapshot(db, group.id, term)
            if snapshot:
                print(
                    f"  семестр {term}: {snapshot.lessons_count} строк, "
                    f"{snapshot.weeks_count} недель, обновлено {snapshot.fetched_at:%d.%m %H:%M}"
                )
            elif weeks:
                print(f"  семестр {term}: {len(weeks)} недель")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Управление приложением «Староста»",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="запустить веб-сервер для локальной сети")
    p_serve.add_argument("--host", default=None, help="0.0.0.0 или 127.0.0.1")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--reload", action="store_true", help="автоперезагрузка при разработке")
    p_serve.set_defaults(func=cmd_serve)

    p_stop = sub.add_parser("stop", help="остановить запущенный веб-сервер")
    p_stop.add_argument("--port", type=int, default=None)
    p_stop.set_defaults(func=cmd_stop)

    p_init = sub.add_parser("init-db", help="создать таблицы")
    p_init.set_defaults(func=cmd_init_db)

    p_seed = sub.add_parser("seed", help="загрузить демо-группу 24ИТ-2")
    p_seed.add_argument("--term", type=int, choices=(1, 2), default=1)
    p_seed.add_argument("--no-attendance", action="store_true", help="не генерировать посещаемость")
    p_seed.add_argument("--live", action="store_true", help="после демо обновить расписание с сайта")
    p_seed.set_defaults(func=cmd_seed)

    p_sync = sub.add_parser("sync", help="обновить расписание группы")
    p_sync.add_argument("--group", default="24ИТ-2")
    p_sync.add_argument("--term", type=int, choices=(1, 2), default=1)
    p_sync.add_argument("--force", action="store_true", help="импортировать даже без изменений")
    p_sync.set_defaults(func=cmd_sync)

    p_sync_all = sub.add_parser(
        "sync-all", help="обновить устаревшие расписания всех групп"
    )
    p_sync_all.add_argument("--hours", type=int, default=None, help="порог свежести в часах")
    p_sync_all.add_argument("--force", action="store_true", help="обновить даже свежие")
    p_sync_all.add_argument("--term", type=int, choices=(1, 2), default=None)
    p_sync_all.add_argument(
        "--delay", type=float, default=None, help="пауза между запросами, секунд"
    )
    p_sync_all.set_defaults(func=cmd_sync_all)

    p_group = sub.add_parser("create-group", help="создать пустую группу")
    p_group.add_argument("--code", required=True)
    p_group.add_argument("--name", default=None)
    p_group.set_defaults(func=cmd_create_group)

    p_show = sub.add_parser("show", help="показать состояние группы")
    p_show.add_argument("--group", default="24ИТ-2")
    p_show.set_defaults(func=cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
