from __future__ import annotations

import random
from datetime import time, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import BASE_DIR
from ..constants import (
    AttendanceStatus,
    DocStatus,
    DocType,
    Funding,
    OccurrenceStatus,
    Role,
)
from ..models import Attendance, Group, Lesson, LessonOccurrence, Membership, User
from ..utils import now_local, today_local
from . import attendance as attendance_service
from . import documents as documents_service
from . import groups as groups_service
from .polessu import ScheduleFetchError, ScheduleParseError, load_payload_file
from .schedule_sync import import_parsed, sync_group

DEMO_GROUP = "24ИТ-2"

DEMO_STUDENTS = (
    "Иванова Анна Сергеевна",
    "Ковальчук Дмитрий Олегович",
    "Мороз Екатерина Павловна",
    "Шевчук Артём Игоревич",
    "Левчук Мария Викторовна",
    "Гаврилюк Никита Сергеевич",
    "Бондарь Ольга Андреевна",
    "Ткачук Владислав Дмитриевич",
    "Мельник София Александровна",
    "Савчук Роман Витальевич",
    "Данилюк Алина Игоревна",
    "Кравчук Павел Олегович",
)


def payload_path(term: int) -> Path:
    return BASE_DIR / "app" / "fixtures" / f"schedule_24it2_term{term}.json"


def seed_demo(
    db: Session,
    *,
    user: User,
    term: int = 1,
    with_attendance: bool = True,
) -> Group:
    existing = db.scalar(select(Group).where(Group.code == DEMO_GROUP))
    if existing is not None:
        membership = groups_service.find_membership(db, user.id, existing.id)
        if membership is None:
            has_head = db.scalar(
                select(Membership.id).where(
                    Membership.group_id == existing.id,
                    Membership.role == Role.HEAD,
                    Membership.status == "active",
                )
            )
            db.add(
                Membership(
                    group_id=existing.id,
                    user_id=user.id,
                    full_name=user.full_name,
                    role=Role.HEAD if has_head is None else Role.DEPUTY,
                    status="active",
                )
            )
            db.commit()
        return existing

    group, _head = groups_service.create_group(
        db,
        user=user,
        code=DEMO_GROUP,
        name="24ИТ-2 (демо)",
        current_term=term,
    )

    students: list[Membership] = []
    for index, name in enumerate(DEMO_STUDENTS):
        member = groups_service.add_member(
            db,
            group=group,
            full_name=name,
            role=Role.DEPUTY if index == 0 else Role.STUDENT,
            actor=user,
        )
        member.funding = Funding.PAID if index in (1, 4, 7) else Funding.BUDGET
        students.append(member)
    db.commit()

    _load_schedule(db, group, term, user)
    if with_attendance:
        _seed_attendance(db, group, user, days=21)
    _seed_documents(db, group, user, students)
    return group


def _load_schedule(db: Session, group: Group, term: int, user: User) -> None:
    path = payload_path(term)
    if path.exists():
        parsed = load_payload_file(path)
        parsed.group = group.code
        import_parsed(db, group, term, parsed, actor=user)
        return
    try:
        sync_group(db, group, term, actor=user, force=True)
    except (ScheduleFetchError, ScheduleParseError):
        return


def _seed_attendance(db: Session, group: Group, user: User, *, days: int) -> None:
    today = today_local()
    start = today - timedelta(days=days)
    occurrences = list(
        db.scalars(
            select(LessonOccurrence)
            .join(Lesson, LessonOccurrence.lesson_id == Lesson.id)
            .where(
                LessonOccurrence.group_id == group.id,
                LessonOccurrence.date >= start,
                LessonOccurrence.date < today,
                LessonOccurrence.status == OccurrenceStatus.SCHEDULED,
            )
            .order_by(LessonOccurrence.date, Lesson.start_time)
        ).all()
    )
    members = attendance_service.roster(db, group.id)
    if not occurrences or not members:
        return

    for occurrence in occurrences:
        for member in members:
            rnd = random.Random(occurrence.id * 100_000 + member.id)
            # Присутствие не храним: отсутствие — исключение из правила.
            if rnd.random() < 0.12:
                db.add(
                    Attendance(
                        occurrence_id=occurrence.id,
                        member_id=member.id,
                        status=AttendanceStatus.ABSENT,
                        source="head",
                        marked_by_id=user.id,
                        marked_at=now_local(),
                    )
                )
    db.commit()


def _seed_documents(
    db: Session, group: Group, user: User, students: list[Membership]
) -> None:
    today = today_local()
    if students:
        documents_service.create_document(
            db,
            group=group,
            author=user,
            type_=DocType.ORDER,
            number="185",
            student_ids=[student.id for student in students[:2]],
            issued_at=today,
            valid_from=today - timedelta(days=2),
            valid_to=today + timedelta(days=1),
            valid_from_time=time(8, 0),
            valid_to_time=time(23, 59),
            body=(
                "Освободить студентов от занятий на указанный период "
                "по уважительной причине."
            ),
            meta={"demo": True},
            status=DocStatus.ACTIVE,
        )
    if len(students) > 2:
        documents_service.create_document(
            db,
            group=group,
            author=user,
            type_=DocType.CERTIFICATE,
            title="Справка по семейным обстоятельствам",
            student_id=students[1].id,
            issued_at=today - timedelta(days=3),
            valid_from=today - timedelta(days=2),
            valid_to=today + timedelta(days=3),
            reason="Семейные обстоятельства",
            body="Период отсутствия по семейным обстоятельствам.",
            meta={"demo": True},
            status=DocStatus.ACTIVE,
        )
        documents_service.create_document(
            db,
            group=group,
            author=user,
            type_=DocType.CERTIFICATE,
            title="Справка о болезни",
            student_id=students[2].id,
            issued_at=today - timedelta(days=10),
            valid_from=today - timedelta(days=10),
            valid_to=today - timedelta(days=2),
            reason="Медицинская справка",
            body="Период освобождения от занятий по справке. Диагноз не хранится.",
            meta={"demo": True},
            status=DocStatus.ACTIVE,
        )
        documents_service.create_document(
            db,
            group=group,
            author=user,
            type_=DocType.STATEMENT,
            title="Заявление на пересдачу лабораторной работы",
            student_id=students[3].id,
            issued_at=today - timedelta(days=1),
            body=(
                "Прошу разрешить пересдачу лабораторной работы №4 по "
                "высокоуровневым языкам программирования по уважительной причине."
            ),
            meta={"demo": True},
        )
