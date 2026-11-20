from __future__ import annotations

from datetime import date, time
from io import BytesIO
from types import SimpleNamespace

import pytest
from docx import Document as DocxDocument
from docx.oxml.ns import qn
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


def _grid(table) -> list[int]:
    return [
        int(column.get(qn("w:w")))
        for column in table._tbl.tblGrid.findall(qn("w:gridCol"))
    ]


def _spans(table, row_index: int) -> list[int]:
    spans = []
    for cell in table.rows[row_index]._tr.findall(qn("w:tc")):
        grid_span = cell.tcPr.find(qn("w:gridSpan")) if cell.tcPr is not None else None
        spans.append(int(grid_span.get(qn("w:val"))) if grid_span is not None else 1)
    return spans


def test_docx_reports_match_university_layout():
    group = SimpleNamespace(id=1, code="24ИТ-2")
    member = SimpleNamespace(full_name="Гриценяк Кирилл Вадимович", funding="budget")

    propuski = vedomosti.PropuskiReport(
        group=group,
        year=2025,
        month=10,
        first=date(2025, 10, 1),
        last=date(2025, 10, 31),
        course=3,
        students=[
            SimpleNamespace(
                member=member,
                days={6: 4},
                total_hours=4,
                excused_hours=2,
                absent_hours=2,
            )
        ],
        certificates=[vedomosti.DocumentLine("Потапейко А.Д.", "14.10.2025")],
        statements=[vedomosti.DocumentLine("Демидовец А.А.", "16.10.2025")],
        orders=[vedomosti.DocumentLine("Седляр Я.Ф.", "02.10.2025", "185")],
        paid_work=[vedomosti.PaidWorkLine(member.full_name, "Физика", 2)],
    )
    document = DocxDocument(BytesIO(vedomosti.render_propuski(propuski)))

    assert len(document.tables) == 3
    main, documents, paid = document.tables
    # 3 фиксированные колонки + 31 день + ИТОГО/По уваж./Безуваж.
    assert len(_grid(main)) == 37
    assert _spans(main, 0)[-1] == 2
    assert _spans(documents, 0) == [2, 2, 2]
    assert _spans(documents, 1) == [1, 1, 1, 1, 1, 1]
    assert len(_grid(documents)) == 6
    assert len(_grid(paid)) == 3
    assert documents.rows[2].cells[0].text == "Потапейко А.Д."

    paid_report = vedomosti.PaidReport(
        group=group,
        year=2025,
        month=10,
        course=3,
        groups=[
            vedomosti.PaidGroup(
                subject="Физическая культура",
                teacher="Снитко Алина Николаевна",
                entries=[
                    vedomosti.PaidRow(
                        member=member,
                        day=date(2025, 10, 6),
                        kind="lab",
                        hours=2,
                        amount=18.47,
                    )
                ],
            )
        ],
    )
    document = DocxDocument(BytesIO(vedomosti.render_paid(paid_report)))
    table = document.tables[0]
    assert len(_grid(table)) == 13
    assert len(table.rows) == 26
    assert _spans(table, 0) == [6, 1, 6]
    assert _spans(table, 1) == [6, 1, 6]
    assert _spans(table, 2) == [2, 2, 5, 2, 2]
    assert _spans(table, 4) == [1, 2, 2, 3, 2, 2, 1]
    assert table.rows[6].cells[0].text == "1."
    assert table.rows[6].cells[1].text == "Гриценяк Кирилл Вадимович"
    assert table.rows[6].cells[3].text == "06.10.2025"
    assert table.rows[6].cells[5].text == "18,47"
