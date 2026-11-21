from __future__ import annotations

import pytest
from sqlalchemy import select

from app.constants import AttendanceStatus, DocStatus, DocType, OccurrenceStatus
from app.models import Attendance, Group, LessonOccurrence, Membership, User
from app.services import attendance as attendance_service
from app.services import documents as documents_service


def _occurrences(db, group_id: int) -> list[LessonOccurrence]:
    return list(
        db.scalars(
            select(LessonOccurrence)
            .where(LessonOccurrence.group_id == group_id)
            .order_by(LessonOccurrence.date, LessonOccurrence.id)
        ).all()
    )


def test_default_present_and_absent(db, seeded):
    occurrences = _occurrences(db, seeded["group_id"])
    first = occurrences[0]
    member = db.get(Membership, seeded["members"][0])
    user = db.get(User, seeded["user_id"])
    assert member is not None and user is not None

    same_day = [o for o in occurrences if o.date == first.date]
    total = len(same_day)

    # Без отметок все считаются присутствующими.
    stats = attendance_service.summary(
        db, seeded["group_id"], first.date, first.date
    )
    item = next(s for s in stats if s.member.id == member.id)
    assert item.present == total
    assert item.absent == 0
    assert item.percent == 100

    attendance_service.mark(
        db,
        occurrence=first,
        member=member,
        status=AttendanceStatus.ABSENT,
        actor=user,
    )
    stats = attendance_service.summary(
        db, seeded["group_id"], first.date, first.date
    )
    item = next(s for s in stats if s.member.id == member.id)
    assert item.absent == 1
    assert item.present == total - 1
    assert item.percent == round((total - 1) * 100 / total)


def test_toggle_marks_only_absent(db, seeded):
    occurrence = _occurrences(db, seeded["group_id"])[0]
    member = db.get(Membership, seeded["members"][0])
    user = db.get(User, seeded["user_id"])

    assert (
        attendance_service.toggle(
            db, occurrence=occurrence, member=member, actor=user
        )
        == AttendanceStatus.ABSENT
    )
    assert (
        attendance_service.toggle(
            db, occurrence=occurrence, member=member, actor=user
        )
        == AttendanceStatus.PRESENT
    )


def test_certificate_auto_excuses_and_revoke(db, seeded):
    occurrence = _occurrences(db, seeded["group_id"])[0]
    member = db.get(Membership, seeded["members"][1])
    user = db.get(User, seeded["user_id"])
    group = db.get(Group, seeded["group_id"])

    document = documents_service.create_document(
        db,
        group=group,
        author=user,
        type_=DocType.CERTIFICATE,
        title="Справка",
        student_id=member.id,
        valid_from=occurrence.date,
        valid_to=occurrence.date,
        reason="Семейные обстоятельства",
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
    assert (document.meta or {}).get("excused_lessons", 0) >= 1

    documents_service.set_status(
        db, document=document, status=DocStatus.REVOKED, actor=user
    )
    db.refresh(row)
    assert row.status == AttendanceStatus.ABSENT
    assert row.document_id is None


def test_cancelled_occurrence_cannot_be_marked(db, seeded):
    occurrence = _occurrences(db, seeded["group_id"])[0]
    occurrence.status = OccurrenceStatus.CANCELLED
    db.commit()
    member = db.get(Membership, seeded["members"][0])

    with pytest.raises(ValueError):
        attendance_service.mark(
            db,
            occurrence=occurrence,
            member=member,
            status=AttendanceStatus.PRESENT,
        )


def test_subject_summary_returns_subjects(db, seeded):
    occurrences = _occurrences(db, seeded["group_id"])
    first, last = occurrences[0], occurrences[-1]
    member = db.get(Membership, seeded["members"][0])
    user = db.get(User, seeded["user_id"])
    attendance_service.mark(
        db,
        occurrence=first,
        member=member,
        status=AttendanceStatus.ABSENT,
        actor=user,
    )

    subjects = attendance_service.subject_summary(
        db, seeded["group_id"], first.date, last.date
    )
    assert subjects
    assert subjects[0]["name"]
    assert subjects[0]["percent"] is not None


def test_excluded_subject_is_not_counted(db, seeded):
    occurrences = [
        occurrence
        for occurrence in _occurrences(db, seeded["group_id"])
        if occurrence.status == OccurrenceStatus.SCHEDULED
    ]
    info = next(
        occurrence
        for occurrence in occurrences
        if occurrence.lesson.subject.name == "Информационный час"
    )
    day = info.date
    same_day = [occurrence for occurrence in occurrences if occurrence.date == day]
    info_count = sum(
        1
        for occurrence in same_day
        if occurrence.lesson.subject_id == info.lesson.subject_id
    )
    member = db.get(Membership, seeded["members"][0])
    user = db.get(User, seeded["user_id"])
    group_id = seeded["group_id"]

    attendance_service.mark(
        db,
        occurrence=info,
        member=member,
        status=AttendanceStatus.ABSENT,
        actor=user,
    )
    before = next(
        item
        for item in attendance_service.summary(db, group_id, day, day)
        if item.member.id == member.id
    )
    assert before.absent == 1
    assert before.total == len(same_day)

    attendance_service.set_subject_excluded(
        db,
        group_id=group_id,
        subject_id=info.lesson.subject_id,
        excluded=True,
        actor=user,
    )

    after = next(
        item
        for item in attendance_service.summary(db, group_id, day, day)
        if item.member.id == member.id
    )
    assert after.absent == 0
    assert after.total == before.total - info_count

    # Пара остаётся в расписании, но в журнале её нет и отметить её нельзя.
    views = attendance_service.day_overview(db, group_id, day)
    assert all(view.lesson.subject_id != info.lesson.subject_id for view in views)
    with pytest.raises(ValueError):
        attendance_service.mark(
            db,
            occurrence=info,
            member=member,
            status=AttendanceStatus.ABSENT,
            actor=user,
        )

    names = [
        item["name"]
        for item in attendance_service.subject_summary(db, group_id, day, day)
    ]
    assert "Информационный час" not in names

    matrix = attendance_service.month_matrix(db, group_id, day.year, day.month)
    assert all(
        entry.subject != "Информационный час" for entry in matrix.paid_entries
    )

    # Повторное включение возвращает прежние счётчики: отметки не удалялись.
    attendance_service.set_subject_excluded(
        db,
        group_id=group_id,
        subject_id=info.lesson.subject_id,
        excluded=False,
        actor=user,
    )
    again = next(
        item
        for item in attendance_service.summary(db, group_id, day, day)
        if item.member.id == member.id
    )
    assert again.absent == before.absent
    assert again.total == before.total
