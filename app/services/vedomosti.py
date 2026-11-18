"""Печатные ведомости: пропуски за месяц и платные отработки."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from io import BytesIO

from docx import Document as DocxDocument
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import DocStatus, DocType
from ..models import Document, Group
from ..utils import course_for_code, format_date_short, short_name
from . import attendance as attendance_service

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
    return f"{format_date_short(start)} - {format_date_short(end)}"


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
    return report


# --------------------------------------------------------------------------- #
# DOCX Generation Helpers
# --------------------------------------------------------------------------- #

def _new_document(*, landscape: bool, base_size: int = 9) -> DocxDocument:
    document = DocxDocument()
    section = document.sections[0]
    if landscape:
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = (section.page_height, section.page_width)
        section.left_margin = Cm(1)
        section.right_margin = Cm(1)
    else:
        section.left_margin = Cm(1.5)
        section.right_margin = Cm(1)
        section.top_margin = Cm(1)
        section.bottom_margin = Cm(1)
        
    style = document.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(base_size)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), "Times New Roman")
    rfonts.set(qn("w:cs"), "Times New Roman")
    return document


def _paragraph(document, text: str = "", *, size: int = 10, bold: bool = False, italic: bool = False, align=WD_ALIGN_PARAGRAPH.CENTER, space_before: float = 0, space_after: float = 0):
    paragraph = document.add_paragraph()
    paragraph.alignment = align
    paragraph.paragraph_format.space_before = Pt(space_before)
    paragraph.paragraph_format.space_after = Pt(space_after)
    run = paragraph.add_run(text)
    run.bold = bold
    run.italic = italic
    run.font.size = Pt(size)
    run.font.name = "Times New Roman"
    return paragraph


def _title(document, text: str, *, size: int = 13) -> None:
    _paragraph(document, text, size=size, bold=True)


def _subtitle(document, text: str, *, size: int = 11, bold: bool = False) -> None:
    _paragraph(document, text, size=size, bold=bold)


def _caption(document, text: str) -> None:
    _paragraph(document, text, size=7, italic=True, space_after=2)


def _cell(cell, text, *, bold: bool = False, size: int = 8, align=WD_ALIGN_PARAGRAPH.CENTER) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.alignment = align
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    run = paragraph.add_run("" if text is None else str(text))
    run.bold = bold
    run.font.size = Pt(size)
    run.font.name = "Times New Roman"
    cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER


def _table(document, rows: int, cols: int):
    table = document.add_table(rows=rows, cols=cols)
    table.style = "Table Grid"
    table.autofit = True
    return table


def _money(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _merged_header(table, row: int, start: int, end: int, text: str) -> None:
    cell = table.cell(row, start)
    if end > start:
        cell = cell.merge(table.cell(row, end))
    _cell(cell, text, bold=True, size=8)


def _documents_table(document, report: PropuskiReport) -> None:
    """Справки, заявления и распоряжения — как в ведомости деканата."""
    _paragraph(
        document,
        "Справки, заявления и распоряжения",
        size=10,
        bold=True,
        align=WD_ALIGN_PARAGRAPH.LEFT,
        space_before=8,
        space_after=3,
    )
    table = _table(document, rows=2, cols=6)
    _merged_header(table, 0, 0, 1, "Справки")
    _merged_header(table, 0, 2, 3, "Заявления")
    _merged_header(table, 0, 4, 5, "Распоряжения")
    
    headers = ("Ф.И.О.", "Дата", "Ф.И.О.", "Дата", "Ф.И.О.", "Номер и дата")
    for index, label in enumerate(headers):
        _cell(table.cell(1, index), label, bold=True, size=8)

    rows_count = max(1, len(report.certificates), len(report.statements), len(report.orders))
    
    for index in range(rows_count):
        cells = table.add_row().cells
        blocks = (
            (report.certificates, 0),
            (report.statements, 2),
            (report.orders, 4),
        )
        for lines, column in blocks:
            line = lines[index] if index < len(lines) else None
            _cell(cells[column], line.student if line else "", size=8, align=WD_ALIGN_PARAGRAPH.LEFT)
            
            if line is None:
                value = ""
            elif column == 4:
                value = f"{line.period} №{line.number or '___'}"
            else:
                value = line.period
            _cell(cells[column + 1], value, size=8)

    widths = (3.4, 3.2, 3.4, 3.0, 3.4, 4.2)
    for row in table.rows:
        for index, width in enumerate(widths):
            row.cells[index].width = Cm(width)


def _paid_work_table(document, report: PropuskiReport) -> None:
    """Нижняя таблица отработки в точности как в шаблоне."""
    course_str = str(report.course) if report.course else "_______"
    _paragraph(
        document,
        f"ВЕДОМОСТЬ УЧЕТА ПЛАТНЫХ ОТРАБОТОК ЗА {report.month_name} {report.year} г., курс {course_str} Группа {report.group.code}",
        size=10,
        bold=True,
        align=WD_ALIGN_PARAGRAPH.CENTER,
        space_before=12,
        space_after=6,
    )
    
    table = _table(document, rows=1, cols=3)
    headers = ("Ф.И.О. студента", "Дисциплина", "Количество часов отработки")
    for index, label in enumerate(headers):
        _cell(table.rows[0].cells[index], label, bold=True, size=9)

    if not report.paid_work:
        cells = table.add_row().cells
        _cell(cells[0], "—", size=9)
        _cell(cells[1], "Пропусков без уважительной причины нет", size=9, align=WD_ALIGN_PARAGRAPH.LEFT)
        _cell(cells[2], "", size=9)
        return

    for line in report.paid_work:
        cells = table.add_row().cells
        _cell(cells[0], line.student, size=9, align=WD_ALIGN_PARAGRAPH.LEFT)
        _cell(cells[1], line.subject, size=9, align=WD_ALIGN_PARAGRAPH.LEFT)
        _cell(cells[2], line.hours, size=9)

    widths = (6.0, 10.0, 5.0)
    for row in table.rows:
        for index, width in enumerate(widths):
            row.cells[index].width = Cm(width)


def _signatures(document) -> None:
    _paragraph(document, "", size=8, space_before=6)
    _paragraph(document, "Дата _______________", size=9, align=WD_ALIGN_PARAGRAPH.LEFT)
    _paragraph(document, "Староста: _____________ ___________________", size=9, align=WD_ALIGN_PARAGRAPH.LEFT)
    _paragraph(document, "        (Ф.И.О.)", size=7, align=WD_ALIGN_PARAGRAPH.LEFT)
    _paragraph(document, "Куратор: _______________ ___________________", size=9, align=WD_ALIGN_PARAGRAPH.LEFT)
    _paragraph(document, "          (Ф.И.О.)", size=7, align=WD_ALIGN_PARAGRAPH.LEFT)


def render_propuski(report: PropuskiReport) -> bytes:
    document = _new_document(landscape=True, base_size=8)
    _title(document, "ВЕДОМОСТЬ", size=14)
    _subtitle(document, "учета учебных часов, пропущенных студентами", size=11)
    _subtitle(document, f"за {report.month_name.lower()} месяц {report.year} г.", size=11)
    
    course = str(report.course) if report.course else "_______"
    _subtitle(document, f"Курс {course} ГРУППА {report.group.code}", size=11)
    _paragraph(document, "", size=8)

    days = report.days
    cols = 3 + days + 3
    table = _table(document, rows=2, cols=cols)
    
    fixed = ("№ п/п", "Дата ФИО", "форма обучения\n(бюджет/платно)")
    for index, label in enumerate(fixed):
        merged = table.cell(0, index).merge(table.cell(1, index))
        _cell(merged, label, bold=True, size=8)

    for offset in range(days):
        column = 3 + offset
        merged = table.cell(0, column).merge(table.cell(1, column))
        _cell(merged, str(offset + 1), bold=True, size=7)

    total_col = 3 + days
    merged = table.cell(0, total_col).merge(table.cell(1, total_col))
    _cell(merged, "ИТОГО", bold=True, size=8)
    _cell(table.cell(0, total_col + 1), "", bold=True, size=7)
    _cell(table.cell(1, total_col + 1), "По уваж.", bold=True, size=7)
    _cell(table.cell(0, total_col + 2), "", bold=True, size=7)
    _cell(table.cell(1, total_col + 2), "Безуваж.", bold=True, size=7)

    funding_short = {"budget": "Б", "paid": "П"}
    for number, item in enumerate(report.students, start=1):
        row = table.add_row().cells
        _cell(row[0], number, size=8)
        _cell(row[1], short_name(item.member.full_name), size=8, align=WD_ALIGN_PARAGRAPH.LEFT)
        _cell(row[2], funding_short.get(item.member.funding, "Б"), size=8)
        
        for day in range(1, days + 1):
            hours = item.days.get(day)
            _cell(row[2 + day], hours or "", size=7)
            
        _cell(row[total_col], item.total_hours or "", size=8)
        _cell(row[total_col + 1], item.excused_hours or "", size=8)
        _cell(row[total_col + 2], item.absent_hours or "", size=8)

    if not report.students:
        row = table.add_row().cells
        _cell(row[0], "—", size=8)
        _cell(row[1], "В группе пока нет студентов", size=8, align=WD_ALIGN_PARAGRAPH.LEFT)
        for column in range(2, cols):
            _cell(row[column], "", size=8)

    totals_row = table.add_row().cells
    _cell(totals_row[0], "", size=8)
    _cell(totals_row[1], "ИТОГО по группе", bold=True, size=8)
    _cell(totals_row[2], "", size=8)
    for day in range(1, days + 1):
        hours = sum(item.days.get(day, 0) for item in report.students)
        _cell(totals_row[2 + day], hours or "", bold=True, size=7)
        
    _cell(totals_row[total_col], report.total_hours or "", bold=True, size=8)
    _cell(totals_row[total_col + 1], report.excused_hours or "", bold=True, size=8)
    _cell(totals_row[total_col + 2], report.absent_hours or "", bold=True, size=8)

    _documents_table(document, report)
    _paid_work_table(document, report)
    _signatures(document)

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def render_paid(report: PaidReport) -> bytes:
    document = _new_document(landscape=False, base_size=9)
    
    if not report.groups:
        _title(document, "Ведомость учета платных отработок по дисциплине")
        _subtitle(document, f"за {report.month_name} {report.year} г. группы {report.group.code}")
        _paragraph(document, "", size=9)
        _paragraph(document, "Пропусков занятий без уважительной причины за месяц нет.", size=10, align=WD_ALIGN_PARAGRAPH.LEFT)
        buffer = BytesIO()
        document.save(buffer)
        return buffer.getvalue()

    headers = (
        "№ п/п", "ФИО студента полностью", "Дата пропуска занятия",
        "Сумма к оплате*, руб", "Номер операции в ЕРИП",
        "Дата совершения оплаты", "Подпись преподавателя об отработке/дата сдачи"
    )
    row_widths = (1.0, 4.8, 2.4, 2.2, 2.4, 2.2, 3.6)

    for index, group in enumerate(report.groups):
        if index:
            document.add_page_break()
            
        _title(document, "Ведомость учета платных отработок по дисциплине")
        _subtitle(document, group.subject, size=12, bold=True)
        _caption(document, "название дисциплины")
        _subtitle(document, group.teacher, size=11, bold=True)
        _caption(document, "ФИО преподавателя")
        _subtitle(document, f"за {report.month_name} {report.year} г. группы {report.group.code}", size=10)
        _paragraph(document, "", size=6)

        table = _table(document, rows=2, cols=len(headers))
        for column, label in enumerate(headers):
            _cell(table.cell(0, column), label, bold=True, size=8)
            _cell(table.cell(1, column), column + 1, bold=True, size=8)

        for row in table.rows:
            for column, width in enumerate(row_widths):
                row.cells[column].width = Cm(width)

        for number, entry in enumerate(group.entries, start=1):
            row = table.add_row().cells
            _cell(row[0], f"{number}.", size=8)
            _cell(row[1], entry.member.full_name, size=8, align=WD_ALIGN_PARAGRAPH.LEFT)
            _cell(row[2], format_date_short(entry.day), size=8)
            _cell(row[3], _money(entry.amount), size=8)
            _cell(row[4], "", size=8)
            _cell(row[5], "", size=8)
            _cell(row[6], "", size=8)

        _paragraph(
            document,
            "*Если занятие пропущено по уважительной причине, делается отметка «без оплаты»",
            size=7,
            italic=True,
            align=WD_ALIGN_PARAGRAPH.LEFT,
            space_before=4,
        )

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()