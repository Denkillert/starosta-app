from __future__ import annotations

import secrets
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..constants import Funding, MemberStatus, Role
from ..models import Group, Membership, User
from . import audit


class GroupError(ValueError):
    pass


INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_invite_code(db: Session, length: int = 6) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(length))
        if not db.scalar(select(Group.id).where(Group.invite_code == code)):
            return code
    raise GroupError("Не удалось сгенерировать код приглашения")


def ensure_user(
    db: Session,
    *,
    telegram_id: int | None,
    full_name: str,
    username: str | None = None,
) -> User:
    user = None
    if telegram_id is not None:
        user = db.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(
            telegram_id=telegram_id,
            full_name=full_name.strip() or "Пользователь",
            username=username,
        )
        db.add(user)
        db.flush()
        return user

    if full_name and user.full_name != full_name:
        user.full_name = full_name.strip()
    if username and user.username != username:
        user.username = username
    return user


def create_group(
    db: Session,
    *,
    user: User,
    code: str,
    name: str | None = None,
    university: str = "ПолесГУ",
    current_term: int = 1,
) -> tuple[Group, Membership]:
    code = " ".join(code.split())
    if not code:
        raise GroupError("Укажите номер группы, например 24ИТ-2")
    if len(code) > 32:
        raise GroupError("Слишком длинный номер группы")
    if db.scalar(select(Group.id).where(Group.code == code)):
        raise GroupError("Группа с таким номером уже существует")

    group = Group(
        code=code,
        name=name or code,
        university=university,
        invite_code=generate_invite_code(db),
        current_term=current_term,
    )
    db.add(group)
    db.flush()

    membership = Membership(
        group_id=group.id,
        user_id=user.id,
        full_name=user.full_name or "Староста",
        role=Role.HEAD,
        status=MemberStatus.ACTIVE,
    )
    db.add(membership)
    audit.log(
        db,
        action="group.create",
        group_id=group.id,
        actor_id=user.id,
        entity_type="group",
        entity_id=group.id,
        payload={"code": code},
    )
    db.commit()
    db.refresh(group)
    db.refresh(membership)
    return group, membership


def join_by_invite(db: Session, *, user: User, invite_code: str) -> tuple[Group, Membership]:
    invite_code = invite_code.strip().upper()
    group = db.scalar(select(Group).where(Group.invite_code == invite_code))
    if group is None:
        raise GroupError("Код приглашения не найден")

    existing = db.scalar(
        select(Membership).where(
            Membership.group_id == group.id, Membership.user_id == user.id
        )
    )
    if existing:
        if existing.status != MemberStatus.ACTIVE:
            existing.status = MemberStatus.ACTIVE
            db.commit()
        return group, existing

    # Если староста заранее внёс студента по ФИО — привязываем аккаунт.
    candidate = db.scalar(
        select(Membership).where(
            Membership.group_id == group.id,
            Membership.user_id.is_(None),
            func.lower(Membership.full_name) == (user.full_name or "").strip().lower(),
        )
    )
    if candidate is not None:
        candidate.user_id = user.id
        candidate.status = MemberStatus.ACTIVE
        audit.log(
            db,
            action="group.join",
            group_id=group.id,
            actor_id=user.id,
            entity_type="membership",
            entity_id=candidate.id,
        )
        db.commit()
        return group, candidate

    membership = Membership(
        group_id=group.id,
        user_id=user.id,
        full_name=user.full_name or "Студент",
        role=Role.STUDENT,
        status=MemberStatus.ACTIVE,
    )
    db.add(membership)
    audit.log(
        db,
        action="group.join",
        group_id=group.id,
        actor_id=user.id,
        entity_type="membership",
        entity_id=membership.id,
    )
    db.commit()
    return group, membership


def add_member(
    db: Session,
    *,
    group: Group,
    full_name: str,
    role: str = Role.STUDENT,
    actor: User | None = None,
    student_no: str | None = None,
    funding: str = Funding.BUDGET,
) -> Membership:
    full_name = " ".join(full_name.split())
    if len(full_name) < 2:
        raise GroupError("Укажите ФИО студента")
    if role not in Role.ALL:
        raise GroupError("Неизвестная роль")
    if funding not in Funding.ALL:
        raise GroupError("Неизвестная форма обучения")

    duplicate = db.scalar(
        select(Membership).where(
            Membership.group_id == group.id,
            func.lower(Membership.full_name) == full_name.lower(),
        )
    )
    if duplicate:
        raise GroupError("Студент с таким ФИО уже есть в группе")

    membership = Membership(
        group_id=group.id,
        full_name=full_name,
        role=role,
        status=MemberStatus.ACTIVE,
        student_no=student_no,
        funding=funding,
    )
    db.add(membership)
    audit.log(
        db,
        action="member.add",
        group_id=group.id,
        actor_id=actor.id if actor else None,
        entity_type="membership",
        entity_id=membership.id,
        payload={"full_name": full_name, "role": role},
    )
    db.commit()
    return membership


def update_member(
    db: Session,
    *,
    membership: Membership,
    actor: User | None = None,
    role: str | None = None,
    status: str | None = None,
    full_name: str | None = None,
    funding: str | None = None,
) -> Membership:
    if role is not None:
        if role not in Role.ALL:
            raise GroupError("Неизвестная роль")
        if membership.role == Role.HEAD and role != Role.HEAD:
            _ensure_not_last_head(db, membership)
        membership.role = role
    if status is not None:
        if status not in (MemberStatus.ACTIVE, MemberStatus.LEFT):
            raise GroupError("Неизвестный статус участника")
        if membership.role == Role.HEAD and status != MemberStatus.ACTIVE:
            _ensure_not_last_head(db, membership)
        membership.status = status
    if full_name:
        membership.full_name = " ".join(full_name.split())
    if funding is not None:
        if funding not in Funding.ALL:
            raise GroupError("Неизвестная форма обучения")
        membership.funding = funding

    audit.log(
        db,
        action="member.update",
        group_id=membership.group_id,
        actor_id=actor.id if actor else None,
        entity_type="membership",
        entity_id=membership.id,
        payload={"role": role, "status": status, "funding": funding},
    )
    db.commit()
    return membership


def _ensure_not_last_head(db: Session, membership: Membership) -> None:
    heads = db.scalar(
        select(func.count(Membership.id)).where(
            Membership.group_id == membership.group_id,
            Membership.role == Role.HEAD,
            Membership.status == MemberStatus.ACTIVE,
            Membership.id != membership.id,
        )
    )
    if not heads:
        raise GroupError("В группе должен остаться хотя бы один староста")


def memberships_for_user(db: Session, user_id: int) -> list[Membership]:
    return list(
        db.scalars(
            select(Membership)
            .where(
                Membership.user_id == user_id,
                Membership.status == MemberStatus.ACTIVE,
            )
            .order_by(Membership.id)
        ).all()
    )


def membership_for_telegram(
    db: Session, telegram_id: int, group_id: int | None = None
) -> Membership | None:
    stmt = (
        select(Membership)
        .join(User, Membership.user_id == User.id)
        .where(
            User.telegram_id == telegram_id,
            Membership.status == MemberStatus.ACTIVE,
        )
        .order_by(Membership.id)
    )
    if group_id is not None:
        stmt = stmt.where(Membership.group_id == group_id)
    return db.scalar(stmt.limit(1))


def group_members(db: Session, group_id: int) -> list[Membership]:
    return list(
        db.scalars(
            select(Membership)
            .where(Membership.group_id == group_id)
            .order_by(Membership.status, Membership.full_name)
        ).all()
    )


def find_membership(db: Session, user_id: int, group_id: int) -> Membership | None:
    return db.scalar(
        select(Membership).where(
            Membership.user_id == user_id, Membership.group_id == group_id
        )
    )


def today() -> date:
    from ..utils import today_local

    return today_local()
