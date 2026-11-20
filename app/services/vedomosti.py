"""Печатные ведомости: пропуски за месяц и платные отработки."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO

from docx.oxml.ns import qn
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import DocStatus, DocType
from ..models import Document, Group
from ..utils import course_for_code, format_date_short, short_name
from . import attendance as attendance_service
from . import docx_kit

RU_MONTHS_NOM = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)
RU_MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

VISIBLE_DOC_STATUSES = {
    DocType.CERTIFICATE: {DocStatus.ACTIVE, DocStatus.APPROVED},
    DocType.ORDER: {DocStatus.ACTIVE, DocStatus.APPROVED},
    DocType.STATEMENT: {DocStatus.PENDING, DocStatus.APPROVED, DocStatus.ACTIVE},
}

# Бланк рассчитан на 31 день: колонки 1..31 есть всегда, лишние остаются пустыми.
PROPUSKI_DAYS = 31
# Первая колонка с отметкой дня и последние колонки ИТОГО / По уваж. / Безуваж.
PROPUSKI_FIRST_DAY_CELL = 3
PROPUSKI_TOTAL_CELL = 34
# Сколько пустых строк оставляем в бланках, если документов/отработок меньше.
DOCS_MIN_ROWS = 13
PAID_WORK_MIN_ROWS = 33

@dataclass(slots=True)
class DocumentLine:
    student: str
    period: str
    number: str | None = None

@dataclass(slots=True)
class PaidWorkLine:
    student: str
    subject: str
    hours: int

@dataclass(slots=True)
class PropuskiReport:
    group: Group
    year: int
    month: int
    first: date
    last: date
    course: int | None = None
    students: list = field(default_factory=list)
    paid_work: list[PaidWorkLine] = field(default_factory=list)
    certificates: list[DocumentLine] = field(default_factory=list)
    statements: list[DocumentLine] = field(default_factory=list)
    orders: list[DocumentLine] = field(default_factory=list)

    @property
    def month_name(self) -> str:
        return RU_MONTHS_NOM[self.month - 1]

    @property
    def month_genitive(self) -> str:
        return RU_MONTHS_GEN[self.month - 1]

    @property
    def days(self) -> int:
        return self.last.day

    @property
    def total_hours(self) -> int:
        return sum(item.total_hours for item in self.students)

    @property
    def excused_hours(self) -> int:
        return sum(item.excused_hours for item in self.students)

    @property
    def absent_hours(self) -> int:
        return sum(item.absent_hours for item in self.students)

@dataclass(slots=True)
class PaidRow:
    member: object
    day: date
    kind: str
    hours: int
    amount: float

@dataclass(slots=True)
class PaidGroup:
    subject: str
    teacher: str
    entries: list[PaidRow] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(entry.amount for entry in self.entries)

@dataclass(slots=True)
class PaidReport:
    group: Group
    year: int
    month: int
    course: int | None = None
    groups: list[PaidGroup] = field(default_factory=list)

    @property
    def month_name(self) -> str:
        return RU_MONTHS_NOM[self.month - 1]

    @property
    def entries_count(self) -> int:
        return sum(len(group.entries) for group in self.groups)

    @property
    def total(self) -> float:
        return sum(group.total for group in self.groups)


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return first, date.fromordinal(nxt.toordinal() - 1)


def _document_students(document: Document) -> list:
    students = list(document.students or [])
    if not students and document.student is not None:
        students = [document.student]
    return students


def _document_period(document: Document) -> str:
    start = document.valid_from or document.issued_at
    end = document.valid_to or start
    if start is None:
        return "—"
    if end is None or start == end:
        return format_date_short(start)
    # В бланке диапазон пишут без пробелов вокруг тире — иначе не влезает в колонку.
    return f"{format_date_short(start)}-{format_date_short(end)}"


def _overlaps(document: Document, first: date, last: date) -> bool:
    if document.type == DocType.STATEMENT:
        day = document.issued_at
        return bool(day and first <= day <= last)
    start = document.valid_from or document.issued_at
    end = document.valid_to or document.valid_from or document.issued_at
    if start is None:
        return False
    if end is None:
        end = start
    return start <= last and end >= first


def _document_lines(document: Document) -> list[DocumentLine]:
    students = _document_students(document)
    if not students:
        return []

    if document.type == DocType.STATEMENT:
        period = format_date_short(document.issued_at)
    else:
        period = _document_period(document)

    number = document.number if document.type == DocType.ORDER else None
    return [
        DocumentLine(
            student=short_name(student.full_name),
            period=period,
            number=number,
        )
        for student in students
    ]


def propuski_report(db: Session, group: Group, year: int, month: int) -> PropuskiReport:
    first, last = _month_bounds(year, month)
    matrix = attendance_service.month_matrix(db, group.id, year, month)

    report = PropuskiReport(
        group=group,
        year=year,
        month=month,
        first=first,
        last=last,
        course=course_for_code(group.code),
        students=matrix.students,
    )

    work: dict[tuple[int, str], PaidWorkLine] = {}
    for entry in matrix.paid_entries:
        key = (entry.member.id, entry.subject)
        line = work.get(key)
        if line is None:
            line = PaidWorkLine(
                student=short_name(entry.member.full_name),
                subject=entry.subject,
                hours=0,
            )
            work[key] = line
        line.hours += entry.hours

    report.paid_work = sorted(
        work.values(), key=lambda item: (item.student.lower(), item.subject.lower())
    )

    documents = db.scalars(
        select(Document)
        .where(
            Document.group_id == group.id,
            Document.type.in_([DocType.CERTIFICATE, DocType.STATEMENT, DocType.ORDER]),
        )
        .order_by(Document.valid_from, Document.issued_at, Document.id)
    ).all()

    for document in documents:
        allowed = VISIBLE_DOC_STATUSES.get(document.type, set())
        if document.status not in allowed:
            continue
        if not _overlaps(document, first, last):
            continue

        lines = _document_lines(document)
        if document.type == DocType.CERTIFICATE:
            report.certificates.extend(lines)
        elif document.type == DocType.STATEMENT:
            report.statements.extend(lines)
        else:
            report.orders.extend(lines)

    return report


def paid_report(db: Session, group: Group, year: int, month: int) -> PaidReport:
    matrix = attendance_service.month_matrix(db, group.id, year, month)
    report = PaidReport(
        group=group,
        year=year,
        month=month,
        course=course_for_code(group.code),
    )

    grouped: dict[tuple[str, str], PaidGroup] = {}
    rate = float(settings.paid_lesson_rate)
    lessons = max(1, int(settings.academic_hours_per_lesson))

    for entry in matrix.paid_entries:
        key = (entry.subject, entry.teacher or "—")
        bucket = grouped.get(key)
        if bucket is None:
            bucket = PaidGroup(subject=entry.subject, teacher=entry.teacher or "—")
            grouped[key] = bucket

        amount = rate * (entry.hours / lessons)
        bucket.entries.append(
            PaidRow(
                member=entry.member,
                day=entry.day,
                kind=entry.kind,
                hours=entry.hours,
                amount=amount,
            )
        )

    report.groups = sorted(
        grouped.values(), key=lambda item: (item.subject.lower(), item.teacher.lower())
    )
    for paid_group in report.groups:
        paid_group.entries.sort(key=lambda row: (row.day, row.member.full_name))
    return report


# --------------------------------------------------------------------------- #
# DOCX: ведомость пропусков
# --------------------------------------------------------------------------- #

FUNDING_SHORT = {"budget": "Б", "paid": "П"}


def _money(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _order_cell(line: DocumentLine) -> str:
    return f"№{line.number} {line.period}" if line.number else line.period


def _fill_students(table, report: PropuskiReport) -> None:
    """Основная таблица: № / ФИО / форма обучения / дни 1..31 / итоги."""
    rows = docx_kit.table_rows(table)
    if not report.students:
        return  # в бланке остаётся одна пустая строка

    prototype = rows[2]
    docx_kit.drop_rows(table, 2)

    for number, item in enumerate(report.students, start=1):
        tr = docx_kit.append_row(table, prototype)
        cells = docx_kit.row_cells(table, tr)
        docx_kit.set_cell(cells[0], number)
        docx_kit.set_cell(cells[1], short_name(item.member.full_name))
        docx_kit.set_cell(cells[2], FUNDING_SHORT.get(item.member.funding, "Б"))
        for day in range(1, PROPUSKI_DAYS + 1):
            hours = item.days.get(day) if day <= report.days else None
            docx_kit.set_cell(cells[PROPUSKI_FIRST_DAY_CELL + day - 1], hours or "")
        docx_kit.set_cell(cells[PROPUSKI_TOTAL_CELL], item.total_hours)
        docx_kit.set_cell(cells[PROPUSKI_TOTAL_CELL + 1], item.excused_hours or "")
        docx_kit.set_cell(cells[PROPUSKI_TOTAL_CELL + 2], item.absent_hours or "")


def _fill_documents(table, report: PropuskiReport) -> None:
    """Таблица справок, заявлений и распоряжений."""
    rows = docx_kit.table_rows(table)
    prototype = rows[2]
    docx_kit.drop_rows(table, 2)

    row_count = max(
        DOCS_MIN_ROWS,
        len(report.certificates),
        len(report.statements),
        len(report.orders),
    )
    blocks = (
        (report.certificates, 0),
        (report.statements, 2),
        (report.orders, 4),
    )

    for index in range(row_count):
        tr = docx_kit.append_row(table, prototype)
        cells = docx_kit.row_cells(table, tr)
        for lines, offset in blocks:
            line = lines[index] if index < len(lines) else None
            if line is None:
                docx_kit.set_cell(cells[offset], "")
                docx_kit.set_cell(cells[offset + 1], "")
                continue
            docx_kit.set_cell(cells[offset], line.student)
            if offset == 4:
                docx_kit.set_cell(cells[offset + 1], _order_cell(line))
            else:
                docx_kit.set_cell(cells[offset + 1], line.period)


def _fill_paid_work(table, report: PropuskiReport) -> None:
    """Нижняя таблица второй страницы: кто и сколько часов отрабатывает."""
    rows = docx_kit.table_rows(table)
    prototype = rows[1]
    docx_kit.drop_rows(table, 1)

    row_count = max(PAID_WORK_MIN_ROWS, len(report.paid_work))
    for index in range(row_count):
        tr = docx_kit.append_row(table, prototype)
        cells = docx_kit.row_cells(table, tr)
        line = report.paid_work[index] if index < len(report.paid_work) else None
        docx_kit.set_cell(cells[0], line.student if line else "")
        docx_kit.set_cell(cells[1], line.subject if line else "")
        docx_kit.set_cell(cells[2], line.hours if line else "")


def render_propuski(report: PropuskiReport) -> bytes:
    """Ведомость пропусков: страница отметок + страница платных отработок."""
    document = docx_kit.open_template("propuski.docx")
    course = str(report.course) if report.course else " "
    docx_kit.replace_tokens(
        document,
        {
            "MONTH": report.month_name.lower(),
            "YEAR": report.year,
            "COURSE": course,
            "GROUP": report.group.code,
        },
    )

    _fill_students(document.tables[0], report)
    _fill_documents(document.tables[1], report)
    _fill_paid_work(document.tables[2], report)

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# DOCX: ведомость платных отработок по дисциплине
# --------------------------------------------------------------------------- #

PAID_HEADER_ROWS = 6   # шапка бланка


def _paid_page(template, page_break, blocks: list) -> None:
    """Вставляет на страницу блок бланка (заголовок, таблица, сноска)."""
    section = template.element.body.find(qn("w:sectPr"))
    if page_break is not None:
        section.addprevious(copy.deepcopy(page_break))
    for block in blocks:
        section.addprevious(copy.deepcopy(block))


def render_paid(report: PaidReport) -> bytes:
    """Ведомость платных отработок: по одной странице на дисциплину."""
    document = docx_kit.open_template("paid.docx")
    body = document.element.body
    section = body.find(qn("w:sectPr"))
    blocks = [child for child in body.iterchildren() if child is not section]
    for child in blocks:
        body.remove(child)

    page = blocks[:-1]
    page_break = blocks[-1]
    groups = report.groups or [None]

    for index in range(len(groups)):
        _paid_page(document, page_break if index else None, page)

    for group, table in zip(groups, document.tables):
        rows = docx_kit.table_rows(table)
        head = docx_kit.row_cells(table, rows[0])
        docx_kit.set_cell(head[0], group.subject if group else "")
        docx_kit.set_cell(head[2], group.teacher if group else "")

        date_row = docx_kit.row_cells(table, rows[2])
        docx_kit.set_cell(date_row[1], report.month_name)
        docx_kit.set_cell(date_row[3], report.group.code)

        data_rows = rows[PAID_HEADER_ROWS:]
        entries = group.entries if group else []
        donor = docx_kit.row_cells(table, data_rows[0])[1]
        while len(data_rows) < len(entries):
            data_rows.append(docx_kit.append_row(table, data_rows[-1]))

        for index, tr in enumerate(data_rows):
            cells = docx_kit.row_cells(table, tr)
            entry = entries[index] if index < len(entries) else None
            if entry is None:
                for cell in cells:
                    docx_kit.set_cell(cell, "")
                continue
            docx_kit.set_cell(cells[0], f"{index + 1}.", donor=donor)
            docx_kit.set_cell(cells[1], entry.member.full_name, donor=donor)
            docx_kit.set_cell(cells[2], format_date_short(entry.day), donor=donor)
            docx_kit.set_cell(cells[3], _money(entry.amount), donor=donor)
            for cell in cells[4:]:
                docx_kit.set_cell(cell, "")

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()
