from __future__ import annotations

import ipaddress
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import settings
from .constants import MemberStatus, Role
from .db import get_db
from .models import Group, Membership, User
from .security import (
    SESSION_COOKIE,
    session_secret,
    user_from_init_data,
    validate_init_data,
    verify_session,
)
from .services.groups import ensure_user, find_membership

DbSession = Annotated[Session, Depends(get_db)]

# Заголовки, которые появляются только когда запрос идёт через внешний
# туннель (cloudflared, ngrok, reverse-proxy). Прямой вход из LAN их не имеет.
PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip")


def _host_is_local(host: str | None) -> bool:
    """Host указывает на эту машину или локальную сеть, а не на публичный домен."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in ("localhost", "testserver"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Короткое имя без точки (mypc, testserver) — локальное; публичный
        # домен вроде abc.app.lhr.life всегда содержит точку.
        return "." not in host
    return address.is_loopback or address.is_private


def is_proxied(request: Request) -> bool:
    """Запрос пришёл снаружи (туннель/прокси), а не напрямую из локальной сети."""
    if any(request.headers.get(name) for name in PROXY_HEADERS):
        return True
    # SSH-туннели (localhost.run, serveo) не добавляют служебных заголовков,
    # но публичное имя в Host видно всегда: браузер в LAN шлёт IP или
    # короткое имя, а туннель — свой домен.
    return not _host_is_local(request.url.hostname)


def get_current_user(request: Request, db: DbSession) -> User:
    secret = session_secret()
    cookie_user_id = verify_session(
        request.cookies.get(SESSION_COOKIE), secret=secret
    )
    if cookie_user_id is not None:
        cookie_user = db.get(User, cookie_user_id)
        if cookie_user is not None:
            return cookie_user

    init_data = request.headers.get("x-telegram-init-data")
    telegram_id: int | None = None
    full_name = ""
    username: str | None = None

    if init_data:
        data = validate_init_data(init_data)
        if data is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Неверная или устаревшая подпись Telegram initData",
            )
        telegram_id, full_name, username = user_from_init_data(data)
        if telegram_id is None:
            # Иначе ensure_user создавал нового пользователя на каждый запрос
            # и все привязки к группе терялись.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Telegram не передал пользователя в initData",
            )
    elif settings.dev_mode and (settings.dev_mode_public or not is_proxied(request)):
        raw_id = request.headers.get("x-telegram-id")
        telegram_id = int(raw_id) if raw_id and raw_id.isdigit() else settings.dev_telegram_id
        full_name = request.headers.get("x-user-name") or settings.dev_user_name
        username = request.headers.get("x-username")
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Требуется Telegram initData: откройте приложение кнопкой в боте. "
                "Гостевой вход разрешён только из локальной сети."
            ),
        )

    user = ensure_user(
        db,
        telegram_id=telegram_id,
        full_name=full_name or settings.dev_user_name,
        username=username,
    )
    db.commit()
    db.refresh(user)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_optional_user(request: Request, db: DbSession) -> User | None:
    """Пользователь, если он вошёл; иначе None (для ссылок-скачиваний с токеном)."""
    try:
        return get_current_user(request, db)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return None
        raise


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def group_for_user(db: Session, user: User, group_id: int) -> tuple[Group, Membership]:
    group = db.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Группа не найдена")
    membership = find_membership(db, user.id, group_id)
    if membership is None or membership.status != MemberStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="Вы не состоите в этой группе")
    return group, membership


def require_staff(membership: Membership) -> None:
    if membership.role not in Role.STAFF:
        raise HTTPException(status_code=403, detail="Действие доступно старосте и заместителю")
    if membership.status != MemberStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="Участник больше не в группе")
