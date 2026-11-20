from __future__ import annotations

import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..constants import Funding, MemberStatus, Role
from ..models import Attendance, Group, Lesson, LessonOccurrence, Membership, User
from ..utils import lesson_fits_member, subgroup_number
from . import audit


class GroupError(ValueError):
    pass


INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _normalized_name(value: str) -> str:
    return " ".join((value or "").split()).casefold()


def _find_by_name(
    db: Session, group_id: int, full_name: str, *, exclude_id: int | None = None
) -> Membership | None:
    """Поиск студента по ФИО без учёта регистра.

    SQLite lower() не понимает кириллицу, поэтому сравниваем в Python:
    студентов в группе немного.
    """
    target = _normalized_name(full_name)
    for member in db.scalars(
        select(Membership).where(Membership.group_id == group_id)
    ).all():
        if exclude_id is not None and member.id == exclude_id:
            continue
        if _normalized_name(member.full_name) == target:
            return member
    return None


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
        # Имя из Telegram обновляем только у непроверенной заявки: иначе
        # повторный вход по ссылке затирал бы ФИО, которое дал староста.
        if (
            existing.needs_review
            and user.full_name
            and existing.full_name != user.full_name
        ):
            existing.full_name = user.full_name
        db.commit()
        return group, existing

    # Если староста заранее внёс студента по ФИО — привязываем аккаунт.
    candidate = next(
        (
            member
            for member in db.scalars(
                select(Membership).where(
                    Membership.group_id == group.id,
                    Membership.user_id.is_(None),
                )
            ).all()
            if _normalized_name(member.full_name) == _normalized_name(user.full_name or "")
        ),
        None,
    )
    if candidate is not None:
        candidate.user_id = user.id
        candidate.status = MemberStatus.ACTIVE
        candidate.needs_review = False
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
        needs_review=True,
    )
    db.add(membership)
    db.flush()  # нужен id для записи в журнале действий
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


def _validate_subgroup(value) -> int | None:
    """Подгруппа: 1, 2 или «не указана» (None)."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if value in (0, "0"):
        return None
    number = subgroup_number(value)
    if number is None:
        raise GroupError("Подгруппа может быть 1 или 2")
    return number


def add_member(
    db: Session,
    *,
    group: Group,
    full_name: str,
    role: str = Role.STUDENT,
    actor: User | None = None,
    student_no: str | None = None,
    funding: str = Funding.BUDGET,
    subgroup: int | None = None,
) -> Membership:
    full_name = " ".join(full_name.split())
    if len(full_name) < 2:
        raise GroupError("Укажите ФИО студента")
    if len(full_name) > 160:
        raise GroupError("ФИО слишком длинное (максимум 160 символов)")
    if role not in Role.ALL:
        raise GroupError("Неизвестная роль")
    if funding not in Funding.ALL:
        raise GroupError("Неизвестная форма обучения")
    subgroup = _validate_subgroup(subgroup)

    duplicate = _find_by_name(db, group.id, full_name)
    if duplicate:
        raise GroupError("Студент с таким ФИО уже есть в группе")

    membership = Membership(
        group_id=group.id,
        full_name=full_name,
        role=role,
        status=MemberStatus.ACTIVE,
        student_no=student_no,
        funding=funding,
        subgroup=subgroup,
    )
    db.add(membership)
    db.flush()  # нужен id для записи в журнале действий
    audit.log(
        db,
        action="member.add",
        group_id=group.id,
        actor_id=actor.id if actor else None,
        entity_type="membership",
        entity_id=membership.id,
        payload={"full_name": full_name, "role": role, "subgroup": subgroup},
    )
    db.commit()
    return membership


_UNSET = object()


def update_member(
    db: Session,
    *,
    membership: Membership,
    actor: User | None = None,
    role: str | None = None,
    status: str | None = None,
    full_name: str | None = None,
    funding: str | None = None,
    subgroup=_UNSET,
    mark_reviewed: bool = True,
) -> Membership:
    was_head = membership.role == Role.HEAD
    previous_subgroup = membership.subgroup
    if role is not None:
        if role not in Role.ALL:
            raise GroupError("Неизвестная роль")
        if was_head and role != Role.HEAD:
            _ensure_not_last_head(db, membership)
        membership.role = role
    if status is not None:
        if status not in (MemberStatus.ACTIVE, MemberStatus.LEFT):
            raise GroupError("Неизвестный статус участника")
        if was_head and status != MemberStatus.ACTIVE:
            _ensure_not_last_head(db, membership)
        membership.status = status
    if full_name:
        cleaned = " ".join(full_name.split())
        if len(cleaned) < 2:
            raise GroupError("Укажите ФИО студента")
        if len(cleaned) > 160:
            raise GroupError("ФИО слишком длинное (максимум 160 символов)")
        duplicate = _find_by_name(
            db, membership.group_id, cleaned, exclude_id=membership.id
        )
        if duplicate:
            raise GroupError("Студент с таким ФИО уже есть в группе")
        membership.full_name = cleaned
    if funding is not None:
        if funding not in Funding.ALL:
            raise GroupError("Неизвестная форма обучения")
        membership.funding = funding
    if subgroup is not _UNSET:
        membership.subgroup = _validate_subgroup(subgroup)
    if mark_reviewed:
        membership.needs_review = False

    purged = 0
    if membership.subgroup != previous_subgroup:
        # Студент подгруппы 1 не может иметь пропуски по лабораторным
        # подгруппы 2 — старые отметки убираем.
        purged = _purge_foreign_subgroup_marks(db, membership)

    audit.log(
        db,
        action="member.update",
        group_id=membership.group_id,
        actor_id=actor.id if actor else None,
        entity_type="membership",
        entity_id=membership.id,
        payload={
            "full_name": membership.full_name,
            "role": membership.role,
            "status": membership.status,
            "funding": membership.funding,
            "subgroup": membership.subgroup,
            "removed_marks": purged,
        },
    )
    db.commit()
    return membership


def _purge_foreign_subgroup_marks(db: Session, membership: Membership) -> int:
    """Удаляет отметки по парам чужой подгруппы (например, лаб. подгр. 2)."""
    rows = db.execute(
        select(Attendance, Lesson.subgroup)
        .join(LessonOccurrence, Attendance.occurrence_id == LessonOccurrence.id)
        .join(Lesson, LessonOccurrence.lesson_id == Lesson.id)
        .where(Attendance.member_id == membership.id)
    ).all()
    removed = 0
    for row, lesson_subgroup in rows:
        if not lesson_fits_member(lesson_subgroup, membership.subgroup):
            db.delete(row)
            removed += 1
    return removed


def link_pending_to_member(
    db: Session,
    *,
    pending: Membership,
    target: Membership,
    actor: User | None = None,
) -> Membership:
    """Привязывает Telegram-аккаунт из заявки к уже заведённому студенту."""
    if pending.id == target.id:
        raise GroupError("Выберите другого студента")
    if pending.group_id != target.group_id:
        raise GroupError("Студенты из разных групп")
    if pending.user_id is None:
        raise GroupError("У заявки нет Telegram-аккаунта")
    if target.user_id is not None:
        raise GroupError("У этого студента уже привязан Telegram")

    user_id = pending.user_id
    pending_name = pending.full_name
    pending_id = pending.id
    db.delete(pending)
    db.flush()

    target.user_id = user_id
    target.status = MemberStatus.ACTIVE
    target.needs_review = False
    audit.log(
        db,
        action="member.link",
        group_id=target.group_id,
        actor_id=actor.id if actor else None,
        entity_type="membership",
        entity_id=target.id,
        payload={
            "pending_id": pending_id,
            "pending_name": pending_name,
            "user_id": user_id,
        },
    )
    db.commit()
    return target


def _ensure_not_last_head(db: Session, membership: Membership) -> None:
    # Выбывший староста не считается действующим, поэтому его можно
    # понизить/исключить, даже если других старост нет.
    if membership.status != MemberStatus.ACTIVE:
        return
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
