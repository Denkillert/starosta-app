from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.constants import AttendanceStatus
from app.models import Attendance, Group, Lesson, LessonOccurrence, Membership
from app.services import attendance as attendance_service
from app.services import groups as groups_service
from app.services import vedomosti


def _subgroup_occurrence(db, group_id: int, subgroup: str) -> LessonOccurrence | None:
    return db.scalar(
        select(LessonOccurrence)
        .join(Lesson, LessonOccurrence.lesson_id == Lesson.id)
        .where(
            LessonOccurrence.group_id == group_id,
            Lesson.subgroup == subgroup,
        )
        .order_by(LessonOccurrence.date, LessonOccurrence.id)
        .limit(1)
    )


def _common_occurrence(db, group_id: int) -> LessonOccurrence | None:
    return db.scalar(
        select(LessonOccurrence)
        .join(Lesson, LessonOccurrence.lesson_id == Lesson.id)
        .where(
            LessonOccurrence.group_id == group_id,
            Lesson.subgroup.is_(None),
        )
        .order_by(LessonOccurrence.date, LessonOccurrence.id)
        .limit(1)
    )


def _set_subgroup(db, member_id: int, value: int | None) -> Membership:
    member = db.get(Membership, member_id)
    member.subgroup = value
    db.commit()
    return member


def test_mark_rejects_lesson_of_another_subgroup(db, seeded):
    group_id = seeded["group_id"]
    member = _set_subgroup(db, seeded["members"][0], 1)
    occurrence = _subgroup_occurrence(db, group_id, "2 группа")
    if occurrence is None:
        pytest.skip("в расписании нет пар второй подгруппы")

    with pytest.raises(ValueError, match="подгрупп"):
        attendance_service.mark(
            db,
            occurrence=occurrence,
            member=member,
            status=AttendanceStatus.ABSENT,
        )


def test_day_overview_hides_foreign_subgroup_marks(db, seeded):
    group_id = seeded["group_id"]
    first = _set_subgroup(db, seeded["members"][0], 1)
    second = _set_subgroup(db, seeded["members"][1], 2)
    occurrence = _subgroup_occurrence(db, group_id, "1 группа")
    if occurrence is None:
        pytest.skip("в расписании нет пар первой подгруппы")

    views = attendance_service.day_overview(db, group_id, occurrence.date)
    view = next(item for item in views if item.occurrence.id == occurrence.id)
    assert view.applies(first) is True
    assert view.applies(second) is False
    assert first.id in view.marks
    assert second.id not in view.marks


def test_month_matrix_counts_only_own_subgroup_lessons(db, seeded):
    group_id = seeded["group_id"]
    first = _set_subgroup(db, seeded["members"][0], 1)
    second = _set_subgroup(db, seeded["members"][1], 2)

    common = _common_occurrence(db, group_id)
    own = _subgroup_occurrence(db, group_id, "1 группа")
    if common is None or own is None:
        pytest.skip("в расписании нет общих пар или пары первой подгруппы")

    attendance_service.mark(
        db, occurrence=common, member=first, status=AttendanceStatus.ABSENT
    )
    attendance_service.mark(
        db, occurrence=common, member=second, status=AttendanceStatus.ABSENT
    )
    attendance_service.mark(
        db, occurrence=own, member=first, status=AttendanceStatus.ABSENT
    )

    matrix = attendance_service.month_matrix(db, group_id, common.date.year, common.date.month)
    by_id = {item.member.id: item for item in matrix.students}
    assert by_id[first.id].total_hours == 4
    assert by_id[second.id].total_hours == 2

    report = vedomosti.propuski_report(
        db, db.get(Group, group_id), common.date.year, common.date.month
    )
    report_rows = {row.member.id: row for row in report.students}
    assert report_rows[first.id].total_hours == 4
    assert report_rows[second.id].total_hours == 2


def test_subgroup_change_removes_foreign_marks(db, seeded):
    group_id = seeded["group_id"]
    member = _set_subgroup(db, seeded["members"][0], 2)
    occurrence = _subgroup_occurrence(db, group_id, "2 группа")
    if occurrence is None:
        pytest.skip("в расписании нет пар второй подгруппы")

    attendance_service.mark(
        db, occurrence=occurrence, member=member, status=AttendanceStatus.ABSENT
    )
    groups_service.update_member(
        db, membership=member, actor=None, subgroup=1
    )

    left = db.scalar(
        select(func.count(Attendance.id)).where(Attendance.member_id == member.id)
    )
    assert left == 0


def test_invite_join_creates_pending_and_links_to_existing(db, seeded):
    group = db.get(Group, seeded["group_id"])
    newcomer = groups_service.ensure_user(
        db, telegram_id=999001, full_name="Игнат", username="ignat"
    )
    db.commit()

    joined, pending = groups_service.join_by_invite(
        db, user=newcomer, invite_code=group.invite_code
    )
    assert joined.id == group.id
    assert pending.needs_review is True
    # До проверки старостой заявка не попадает в журнал.
    assert pending.id not in {member.id for member in attendance_service.roster(db, group.id)}

    target = groups_service.add_member(
        db, group=group, full_name="Настоящий Студент", subgroup=1
    )
    groups_service.link_pending_to_member(
        db, pending=pending, target=target, actor=None
    )
    db.refresh(target)
    assert target.user_id == newcomer.id
    assert target.needs_review is False
    assert target.subgroup == 1
    assert db.get(Membership, pending.id) is None


def test_invite_join_matches_precreated_student_by_name(db, seeded):
    group = db.get(Group, seeded["group_id"])
    existing = groups_service.add_member(
        db, group=group, full_name="Петров Пётр Петрович", subgroup=2
    )
    user = groups_service.ensure_user(
        db, telegram_id=999002, full_name="Петров Пётр Петрович", username=None
    )
    db.commit()

    _group, membership = groups_service.join_by_invite(
        db, user=user, invite_code=group.invite_code
    )
    assert membership.id == existing.id
    assert membership.user_id == user.id
    assert membership.needs_review is False


def test_rejoin_does_not_overwrite_confirmed_name(db, seeded):
    group = db.get(Group, seeded["group_id"])
    user = groups_service.ensure_user(
        db, telegram_id=999004, full_name="Игнат", username=None
    )
    db.commit()
    _group, pending = groups_service.join_by_invite(
        db, user=user, invite_code=group.invite_code
    )
    groups_service.update_member(
        db,
        membership=pending,
        actor=None,
        full_name="Игнатов Игнат Игнатович",
        subgroup=1,
    )

    _group, again = groups_service.join_by_invite(
        db, user=user, invite_code=group.invite_code
    )
    assert again.id == pending.id
    assert again.full_name == "Игнатов Игнат Игнатович"
    assert again.subgroup == 1


def test_students_page_shows_pending_join(api_client, db, seeded):
    group = db.get(Group, seeded["group_id"])
    user = groups_service.ensure_user(
        db, telegram_id=999003, full_name="Заявкин Иван", username="zayavkin"
    )
    db.commit()
    groups_service.join_by_invite(db, user=user, invite_code=group.invite_code)

    page = api_client.get("/students")
    assert page.status_code == 200, page.text
    assert "Заявки на вступление" in page.text
    assert "Заявкин Иван" in page.text
    assert "Привязать" in page.text


def test_subgroup_number_parses_schedule_labels():
    from app.utils import lesson_fits_member, subgroup_number

    assert subgroup_number("1 группа") == 1
    assert subgroup_number("2") == 2
    assert subgroup_number("") is None
    assert lesson_fits_member("1 группа", 2) is False
    assert lesson_fits_member("2 группа", 2) is True
    assert lesson_fits_member(None, 2) is True
    assert lesson_fits_member("1 группа", None) is True
