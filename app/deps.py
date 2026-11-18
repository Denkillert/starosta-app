from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import settings
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
    elif settings.dev_mode:
        raw_id = request.headers.get("x-telegram-id")
        telegram_id = int(raw_id) if raw_id and raw_id.isdigit() else settings.dev_telegram_id
        full_name = request.headers.get("x-user-name") or settings.dev_user_name
        username = request.headers.get("x-username")
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется Telegram initData",
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


def group_for_user(db: Session, user: User, group_id: int) -> tuple[Group, Membership]:
    group = db.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Группа не найдена")
    membership = find_membership(db, user.id, group_id)
    if membership is None:
        raise HTTPException(status_code=403, detail="Вы не состоите в этой группе")
    return group, membership


def require_staff(membership: Membership) -> None:
    if membership.role not in ("head", "deputy"):
        raise HTTPException(status_code=403, detail="Действие доступно старосте и заместителю")
    if membership.status != "active":
        raise HTTPException(status_code=403, detail="Участник больше не в группе")
