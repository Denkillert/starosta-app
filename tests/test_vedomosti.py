from __future__ import annotations

from datetime import time

import pytest
from sqlalchemy import select

from app.constants import AttendanceStatus, DocStatus, DocType
from app.models import Attendance, Group, LessonOccurrence, Membership, User
from app.services import attendance as attendance_service
from app.services import documents as documents_service
from app.services import vedomosti


def _first_occurrence(db, group_id: int) -> LessonOccurrence:
    return db.scalar(
        select(LessonOccurrence)
        .where(LessonOccurrence.group_id == group_id)
        .order_by(LessonOccurrence.date, LessonOccurrence.id)
    )


def test_order_requires_number_and_excuses_with_time_window(db, seeded):
    occurrence = _first_occurrence(db, seeded["group_id"])
    member = db.get(Membership, seeded["members"][0])
    group = db.get(Group, seeded["group_id"])
    user = db.get(User, seeded["user_id"])

    with pytest.raises(documents_service.DocumentError):
        documents_service.create_document(
            db,
            group=group,
            author=user,
            type_=DocType.ORDER,
            student_id=member.id,
            valid_from=occurrence.date,
            valid_to=occurrence.date,
        )

    document = documents_service.create_document(
        db,
        group=group,
        author=user,
        type_=DocType.ORDER,
        number="185",
        student_id=member.id,
        valid_from=occurrence.date,
        valid_to=occurrence.date,
        valid_from_time=occurrence.lesson.start_time,
        valid_to_time=occurrence.lesson.end_time,
        status=DocStatus.ACTIVE,
    )
    row = db.scalar(
        select(Attendance).where(
            Attendance.occurrence_id == occurrence.id,
            Attendance.member_id == member.id,
        )
    )
    assert row is not None
    assert row.status == AttendanceStatus.EXCUSED
    assert row.document_id == document.id

    documents_service.set_status(
        db, document=document, status=DocStatus.REVOKED, actor=user
    )
    db.refresh(row)
    assert row.status == AttendanceStatus.ABSENT


def test_month_matrix_and_printable_forms(db, seeded):
    occurrence = _first_occurrence(db, seeded["group_id"])
    member = db.get(Membership, seeded["members"][1])
    group = db.get(Group, seeded["group_id"])
    user = db.get(User, seeded["user_id"])

    attendance_service.mark(
        db,
        occurrence=occurrence,
        member=member,
        status=AttendanceStatus.ABSENT,
        actor=user,
    )

    matrix = attendance_service.month_matrix(
        db, group.id, occurrence.date.year, occurrence.date.month
    )
    item = next(row for row in matrix.students if row.member.id == member.id)
    assert item.absent_hours >= 2
    assert matrix.paid_entries

    propuski = vedomosti.propuski_report(
        db, group, occurrence.date.year, occurrence.date.month
    )
    assert propuski.students
    assert propuski.absent_hours >= 2
    assert vedomosti.render_propuski(propuski)[:2] == b"PK"

    paid = vedomosti.paid_report(
        db, group, occurrence.date.year, occurrence.date.month
    )
    assert paid.groups
    assert paid.entries_count >= 1
    assert paid.total > 0
    assert vedomosti.render_paid(paid)[:2] == b"PK"


def test_order_time_window_skips_uncovered_lessons(db, seeded):
    occurrences = list(
        db.scalars(
            select(LessonOccurrence)
            .where(LessonOccurrence.group_id == seeded["group_id"])
            .order_by(LessonOccurrence.date, LessonOccurrence.id)
        ).all()
    )
    first, last = occurrences[0], occurrences[-1]
    member = db.get(Membership, seeded["members"][2])
    group = db.get(Group, seeded["group_id"])
    user = db.get(User, seeded["user_id"])

    # Окно не пересекается с парами выбранного дня.
    documents_service.create_document(
        db,
        group=group,
        author=user,
        type_=DocType.ORDER,
        number="186",
        student_id=member.id,
        valid_from=first.date,
        valid_to=first.date,
        valid_from_time=time(0, 0),
        valid_to_time=time(0, 1),
        status=DocStatus.ACTIVE,
    )
    rows = db.scalars(
        select(Attendance).where(
            Attendance.member_id == member.id,
            Attendance.occurrence_id.in_([first.id, last.id]),
        )
    ).all()
    assert rows == []
