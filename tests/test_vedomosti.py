from __future__ import annotations

from datetime import date, time
from io import BytesIO
from types import SimpleNamespace

import pytest
from docx import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from sqlalchemy import select

from app.constants import AttendanceStatus, DocStatus, DocType
from app.models import Attendance, Group, LessonOccurrence, Membership, User
from app.services import attendance as attendance_service
from app.services import documents as documents_service
from app.services import vedomosti
from app.utils import course_for_code


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


def test_course_is_computed_from_group_code_and_academic_year():
    # «24ИТ-2»: поступили в 2024 году, учебный год начинается в сентябре.
    assert course_for_code("24ИТ-2", date(2026, 11, 21)) == 3
    assert course_for_code("24ИТ-2", date(2026, 9, 1)) == 3
    assert course_for_code("24ИТ-2", date(2026, 8, 31)) == 2
    assert course_for_code("24ИТ-2", date(2027, 6, 30)) == 3
    assert course_for_code("25ИТ-1", date(2026, 9, 1)) == 2
    assert course_for_code("23ИТ-3", date(2026, 11, 21)) == 4
    assert course_for_code("", date(2026, 11, 21)) is None


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
    # «Справки», «Заявления» и «Распоряжения» занимают одинаковую ширину.
    documents_grid = _grid(documents)
    block_widths = [
        sum(documents_grid[0:2]),
        sum(documents_grid[2:4]),
        sum(documents_grid[4:6]),
    ]
    assert max(block_widths) - min(block_widths) <= 1
    assert documents.rows[2].cells[0].text == "Потапейко А.Д."
    assert documents.rows[2].cells[1].text == "14.10.2025"
    assert documents.rows[2].cells[2].text == "Демидовец А.А."
    assert documents.rows[2].cells[3].text == "16.10.2025"
    assert documents.rows[2].cells[4].text == "Седляр Я.Ф."
    assert documents.rows[2].cells[5].text == "№185 02.10.2025"
    # Даты выровнены по центру, а не прижаты влево.
    for column in (1, 3, 5):
        assert (
            documents.rows[2].cells[column].paragraphs[0].alignment
            == WD_ALIGN_PARAGRAPH.CENTER
        )

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


# Ширины колонок бланков (twips) — берутся из образцов университета.
PROPUSKI_GRID = (
    279, 1570, 407,
    279, 249, 249, 249, 249, 249, 249, 249, 249, 249, 248,
    247, 247, 247, 247, 247, 247, 247, 247, 247, 247, 247, 247, 247,
    236, 258, 247, 247, 247, 247, 247,
    458, 522, 409,
)
PAID_GRID = (933, 855, 1343, 1009, 263, 576, 697, 94, 225, 1056, 965, 514, 1961)


def _font(cell) -> tuple[str | None, float | None, bool, bool]:
    run = cell.paragraphs[0].runs[0]
    return (
        run.font.name,
        run.font.size.pt if run.font.size else None,
        bool(run.bold),
        bool(run.italic),
    )


def _all_text(document) -> str:
    return "\n".join("".join(element.itertext()) for element in document.element.body.iter(qn("w:p")))


def test_propuski_repeats_university_blank():
    group = SimpleNamespace(id=1, code="24ИТ-7")
    member = SimpleNamespace(full_name="Иванов Иван Иванович", funding="paid")
    report = vedomosti.PropuskiReport(
        group=group,
        year=2026,
        month=9,
        first=date(2026, 9, 1),
        last=date(2026, 9, 30),
        course=2,
        students=[
            SimpleNamespace(
                member=member, days={3: 2}, total_hours=2, excused_hours=0, absent_hours=2
            )
        ],
    )
    document = DocxDocument(BytesIO(vedomosti.render_propuski(report)))
    section = document.sections[0]
    assert round(section.page_width.cm, 2) == 21.0
    assert round(section.page_height.cm, 2) == 29.7
    assert (
        round(section.left_margin.cm, 2),
        round(section.right_margin.cm, 2),
        round(section.top_margin.cm, 2),
        round(section.bottom_margin.cm, 2),
    ) == (1.27, 1.27, 0.75, 0.5)

    main = document.tables[0]
    assert tuple(_grid(main)) == PROPUSKI_GRID
    assert len(main.rows) == 3  # шапка в 2 строки + ученик
    assert _font(main.cell(0, 0)) == ("Arial", 7.0, True, True)
    assert main.cell(0, 0).text == "№ п/п"
    assert main.cell(0, 3).text == "1"
    assert main.cell(0, 33).text == "31"
    assert main.cell(0, 34).text == "ИТОГО"
    assert main.cell(1, 35).text == "По уваж."
    assert main.cell(1, 36).text == "Безуваж."
    # строка ученика: номер, ФИО, форма обучения, отметка дня, итоги
    assert [cell.text for cell in main.rows[2].cells[:3]] == ["1", "Иванов И.И.", "П"]
    assert main.rows[2].cells[5].text == "2"
    assert main.rows[2].cells[34].text == "2"
    assert main.rows[2].cells[36].text == "2"

    # шапка ведомости заполнена, токенов не осталось
    text = _all_text(document)
    assert "за сентябрь месяц 2026 г." in text
    assert "24ИТ-7" in text
    assert "{{" not in text

    assert document.tables[1].cell(0, 0).text == "Справки"
    assert document.tables[1].cell(0, 2).text == "Заявления"
    assert document.tables[1].cell(0, 4).text == "Распоряжения"
    assert document.tables[2].cell(0, 0).text == "Ф.И.О. студента"
    assert len(document.tables[1].rows) == 2 + vedomosti.DOCS_MIN_ROWS
    assert len(document.tables[2].rows) == 1 + vedomosti.PAID_WORK_MIN_ROWS
    # разрыв страницы между двумя ведомостями
    assert any(
        br.get(qn("w:type")) == "page"
        for br in document.element.body.findall(".//" + qn("w:br"))
    )


def test_paid_repeats_university_blank():
    group = SimpleNamespace(id=1, code="24ИТ-2")
    member = SimpleNamespace(full_name="Гриценяк Кирилл Вадимович", funding="budget")
    report = vedomosti.PaidReport(
        group=group,
        year=2026,
        month=10,
        course=3,
        groups=[
            vedomosti.PaidGroup(
                subject="Физическая культура",
                teacher="Снитко Алина Николаевна",
                entries=[
                    vedomosti.PaidRow(
                        member=member,
                        day=date(2026, 10, 6),
                        kind="lab",
                        hours=2,
                        amount=18.47,
                    )
                ],
            ),
            vedomosti.PaidGroup(subject="Эконометрика", teacher="Иванов И.И.", entries=[]),
        ],
    )
    document = DocxDocument(BytesIO(vedomosti.render_paid(report)))
    section = document.sections[0]
    assert (
        round(section.left_margin.cm, 2),
        round(section.right_margin.cm, 2),
        round(section.top_margin.cm, 2),
        round(section.bottom_margin.cm, 2),
    ) == (3.0, 1.5, 2.0, 2.0)
    assert document.styles["Normal"].font.name == "Times New Roman"
    assert document.styles["Normal"].font.size.pt == 14

    assert len(document.tables) == 2
    assert all(tuple(_grid(table)) == PAID_GRID for table in document.tables)
    first = document.tables[0]
    assert len(first.rows) == vedomosti.PAID_HEADER_ROWS + 20
    assert first.cell(0, 0).text == "Физическая культура"
    assert first.cell(0, 12).text == "Снитко Алина Николаевна"
    assert first.rows[2].cells[2].text == "Октябрь"
    assert first.rows[2].cells[9].text == "24ИТ-2"
    assert first.rows[6].cells[0].text == "1."
    assert first.rows[6].cells[1].text == "Гриценяк Кирилл Вадимович"
    assert first.rows[6].cells[3].text == "06.10.2026"
    assert first.rows[6].cells[5].text == "18,47"
    assert document.tables[1].cell(0, 0).text == "Эконометрика"
    # страница на каждую дисциплину
    assert len(document.element.body.findall(".//" + qn("w:br"))) == 1

