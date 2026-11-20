"""Печатные ведомости: пропуски за месяц и платные отработки."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from io import BytesIO

from docx import Document as DocxDocument
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_ALIGN_VERTICAL, WD_ROW_HEIGHT_RULE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
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
    for paid_group in report.groups:
        paid_group.entries.sort(key=lambda row: (row.day, row.member.full_name))
    return report


# --------------------------------------------------------------------------- #
# DOCX Generation Helpers
# --------------------------------------------------------------------------- #

CM_PER_TWIP = 1 / 567  # 1 см = 567 twips
TWIPS_PER_CM = 567


def _new_document(*, margins=(1.27, 1.27, 0.75, 0.5), base_size: int = 10) -> DocxDocument:
    """A4 книжная, поля как в ведомостях вуза."""
    document = DocxDocument()
    section = document.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    left, right, top, bottom = margins
    section.left_margin = Cm(left)
    section.right_margin = Cm(right)
    section.top_margin = Cm(top)
    section.bottom_margin = Cm(bottom)

    style = document.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(base_size)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), "Calibri")
    rfonts.set(qn("w:cs"), "Calibri")
    return document


def _content_width(section) -> float:
    return (section.page_width - section.left_margin - section.right_margin) / 360000


def _paragraph(
    document,
    text: str = "",
    *,
    size: int = 10,
    bold: bool = False,
    italic: bool = False,
    font: str = "Calibri",
    align=WD_ALIGN_PARAGRAPH.CENTER,
    space_before: float = 0,
    space_after: float = 0,
):
    paragraph = document.add_paragraph()
    paragraph.alignment = align
    paragraph.paragraph_format.space_before = Pt(space_before)
    paragraph.paragraph_format.space_after = Pt(space_after)
    run = paragraph.add_run(text)
    run.bold = bold
    run.italic = italic
    run.font.size = Pt(size)
    run.font.name = font
    return paragraph


def _title(document, text: str, *, size: int = 12) -> None:
    _paragraph(document, text, size=size, bold=True, italic=True)


def _subtitle(document, text: str, *, size: int = 12, bold: bool = True) -> None:
    _paragraph(document, text, size=size, bold=bold, italic=bold)


def _caption(document, text: str, *, size: int = 9) -> None:
    _paragraph(
        document,
        text,
        size=size,
        align=WD_ALIGN_PARAGRAPH.LEFT,
        space_before=4,
    )


def _cell(
    cell,
    text,
    *,
    bold: bool = False,
    italic: bool = False,
    size: float = 8,
    font: str = "Calibri",
    align=WD_ALIGN_PARAGRAPH.CENTER,
) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.alignment = align
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    run = paragraph.add_run("" if text is None else str(text))
    run.bold = bold
    run.italic = italic
    run.font.size = Pt(size)
    run.font.name = font
    cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER


def _cell_multiline(
    cell,
    lines: list[str],
    *,
    bold: bool = False,
    italic: bool = False,
    size: float = 7,
    font: str = "Arial",
    align=WD_ALIGN_PARAGRAPH.CENTER,
) -> None:
    """Несколько абзацев в одной ячейке — как в бланке вуза."""
    cell.text = ""
    for index, line in enumerate(lines):
        paragraph = cell.paragraphs[0] if index == 0 else cell.add_paragraph()
        paragraph.alignment = align
        paragraph.paragraph_format.space_before = Pt(0)
        paragraph.paragraph_format.space_after = Pt(0)
        run = paragraph.add_run(line)
        run.bold = bold
        run.italic = italic
        run.font.size = Pt(size)
        run.font.name = font
    cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER


def _table(document, rows: int, cols: int, widths: list[float]):
    """Таблица с фиксированными ширинами колонок (в см)."""
    table = document.add_table(rows=rows, cols=cols)
    table.style = "Table Grid"
    table.autofit = False

    # Фиксированная раскладка: Word не будет пересчитывать ширины сам.
    tbl_pr = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tbl_pr.append(layout)

    # Узкие внутренние отступы — иначе 31 колонка не помещается.
    margins = OxmlElement("w:tblCellMar")
    for side, value in (("top", 0), ("left", 15), ("bottom", 0), ("right", 15)):
        element = OxmlElement(f"w:{side}")
        element.set(qn("w:w"), str(value))
        element.set(qn("w:type"), "dxa")
        margins.append(element)
    tbl_pr.append(margins)

    grid = table._tbl.tblGrid.findall(qn("w:gridCol"))
    for grid_col, width in zip(grid, widths):
        grid_col.set(qn("w:w"), str(round(width * TWIPS_PER_CM)))
    for row in table.rows:
        for index, width in enumerate(widths):
            row.cells[index].width = Cm(width)
    return table


def _new_row(table, widths: list[float], *, height: float = 0.42) -> list:
    row = table.add_row()
    row.height = Cm(height)
    row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
    for index, width in enumerate(widths):
        row.cells[index].width = Cm(width)
    return row.cells


def _keep_row(row) -> None:
    """Запрещает перенос строки таблицы на следующую страницу."""
    tr_pr = row._tr.get_or_add_trPr()
    if tr_pr.find(qn("w:cantSplit")) is None:
        tr_pr.append(OxmlElement("w:cantSplit"))


def _money(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _fixed_widths(content: float, fixed: tuple[float, ...], tail: tuple[float, ...], count: int) -> list[float]:
    """Ширины колонок: фиксированные + одинаковые дни + итоговые."""
    day = (content - sum(fixed) - sum(tail)) / max(1, count)
    return [*fixed, *([day] * count), *tail]


# --------------------------------------------------------------------------- #
# Ведомость пропусков
# --------------------------------------------------------------------------- #

PROPUSKI_FIXED = (0.49, 2.60, 0.70)   # № п/п, ФИО, форма обучения
PROPUSKI_TAIL = (0.75, 0.80, 0.80)    # ИТОГО, По уваж., Безуваж.


def _documents_table(document, report: PropuskiReport) -> None:
    content = _content_width(document.sections[0])
    widths = [content * share for share in (0.163, 0.157, 0.163, 0.146, 0.163, 0.208)]
    table = _table(document, rows=2, cols=6, widths=widths)

    _cell(table.cell(0, 0).merge(table.cell(0, 1)), "Справки", bold=True, size=8)
    _cell(table.cell(0, 2).merge(table.cell(0, 3)), "Заявления", bold=True, size=8)
    _cell(table.cell(0, 4).merge(table.cell(0, 5)), "Распоряжения", bold=True, size=8)

    headers = ("Ф.И.О.", "Дата", "Ф.И.О.", "Дата", "Ф.И.О.", "Номер и дата")
    for index, label in enumerate(headers):
        _cell(table.cell(1, index), label, bold=True, size=8)
        _keep_row(table.rows[1])

    # Пустые строки оставляем, чтобы бланк можно было заполнять от руки.
    rows_count = max(
        14,
        len(report.certificates),
        len(report.statements),
        len(report.orders),
    )
    blocks = (
        (report.certificates, 0),
        (report.statements, 2),
        (report.orders, 4),
    )

    for index in range(rows_count):
        cells = _new_row(table, widths)
        for lines, column in blocks:
            line = lines[index] if index < len(lines) else None
            _cell(
                cells[column],
                line.student if line else "",
                size=8,
                align=WD_ALIGN_PARAGRAPH.LEFT,
            )
            if line is None:
                value = ""
            elif column == 4:
                value = f"{line.period} №{line.number or '___'}"
            else:
                value = line.period
            _cell(cells[column + 1], value, size=8)


def _paid_work_table(document, report: PropuskiReport) -> None:
    course_str = str(report.course) if report.course else "_______"
    _paragraph(
        document,
        "ВЕДОМОСТЬ УЧЕТА ПЛАТНЫХ ОТРАБОТОК ЗА "
        f"{report.month_name} {report.year} г., курс {course_str} "
        f"Группа {report.group.code}",
        size=10,
        bold=True,
        space_before=10,
        space_after=4,
    )

    content = _content_width(document.sections[0])
    widths = [content * 0.33, content * 0.55, content * 0.12]
    table = _table(document, rows=1, cols=3, widths=widths)
    for index, label in enumerate(
        ("Ф.И.О. студента", "Дисциплина", "Количество часов отработки")
    ):
        _cell(table.rows[0].cells[index], label, bold=True, size=9)

    if not report.paid_work:
        cells = _new_row(table, widths, height=0.6)
        _cell(cells[0], "—", size=9)
        _cell(
            cells[1],
            "Пропусков без уважительной причины нет",
            size=9,
            align=WD_ALIGN_PARAGRAPH.LEFT,
        )
        _cell(cells[2], "", size=9)
        return

    for line in report.paid_work:
        cells = _new_row(table, widths, height=0.6)
        _cell(cells[0], line.student, size=9, align=WD_ALIGN_PARAGRAPH.LEFT)
        _cell(cells[1], line.subject, size=9, align=WD_ALIGN_PARAGRAPH.LEFT)
        _cell(cells[2], line.hours, size=9)


def _signatures(document) -> None:
    _paragraph(document, "", size=8, space_before=6)
    for label in ("Дата                _______________",):
        _paragraph(document, label, size=10, align=WD_ALIGN_PARAGRAPH.LEFT)
    _paragraph(
        document,
        "Староста:      _______________        ___________________",
        size=10,
        align=WD_ALIGN_PARAGRAPH.LEFT,
    )
    _paragraph(
        document,
        " " * 45 + "(Ф.И.О.)",
        size=9,
        align=WD_ALIGN_PARAGRAPH.LEFT,
    )
    _paragraph(
        document,
        "Куратор:         _______________        ___________________",
        size=10,
        align=WD_ALIGN_PARAGRAPH.LEFT,
    )
    _paragraph(
        document,
        " " * 45 + "(Ф.И.О.)",
        size=9,
        align=WD_ALIGN_PARAGRAPH.LEFT,
    )


def render_propuski(report: PropuskiReport) -> bytes:
    document = _new_document(margins=(1.27, 1.27, 0.75, 0.5), base_size=10)

    _title(document, "ВЕДОМОСТЬ")
    _title(document, "учета учебных часов, пропущенных студентами")
    _title(document, f"за {report.month_name.lower()} месяц {report.year} г.")
    course = str(report.course) if report.course else "_______"
    # Пробелы как в бланке: подписи стоят над своими полями.
    _paragraph(
        document,
        f"Курс       {course}       ГРУППА         {report.group.code}          .",
        size=12,
        bold=True,
        italic=True,
        align=WD_ALIGN_PARAGRAPH.LEFT,
    )
    _paragraph(document, "", size=8)

    days = report.days
    content = _content_width(document.sections[0])
    widths = _fixed_widths(content, PROPUSKI_FIXED, PROPUSKI_TAIL, days)
    cols = 3 + days + 3
    table = _table(document, rows=2, cols=cols, widths=widths)
    for row in table.rows:
        _keep_row(row)
    table.rows[0].height = Cm(0.41)
    table.rows[1].height = Cm(1.54)
    table.rows[1].height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST

    merged = table.cell(0, 0).merge(table.cell(1, 0))
    _cell(merged, "№ п/п", bold=True, italic=True, size=7, font="Arial")

    # В бланке вуза в этой ячейке сверху осталось слово «Дата», снизу — «ФИО».
    merged = table.cell(0, 1).merge(table.cell(1, 1))
    _cell_multiline(merged, ["", "Дата", "", "", "ФИО"])

    merged = table.cell(0, 2).merge(table.cell(1, 2))
    _cell_multiline(
        merged, ["форма", "обучения", "(бюджет/платно)"], bold=True, italic=True
    )

    for offset in range(days):
        column = 3 + offset
        merged = table.cell(0, column).merge(table.cell(1, column))
        _cell(merged, offset + 1, bold=True, italic=True, size=7, font="Arial")

    total_col = 3 + days
    merged = table.cell(0, total_col).merge(table.cell(1, total_col))
    _cell(merged, "ИТОГО", bold=True, italic=True, size=7, font="Arial")

    top = table.cell(0, total_col + 1).merge(table.cell(0, total_col + 2))
    _cell(top, "", size=7, font="Arial")
    _cell(table.cell(1, total_col + 1), "По уваж.", bold=True, italic=True, size=7, font="Arial")
    _cell(table.cell(1, total_col + 2), "Безуваж.", bold=True, italic=True, size=7, font="Arial")

    funding_short = {"budget": "Б", "paid": "П"}
    for number, item in enumerate(report.students, start=1):
        row = _new_row(table, widths)
        _cell(row[0], number, size=7, font="Arial", bold=True, italic=True)
        _cell(
            row[1],
            short_name(item.member.full_name),
            size=10,
            italic=True,
            align=WD_ALIGN_PARAGRAPH.LEFT,
        )
        _cell(row[2], funding_short.get(item.member.funding, "Б"), size=10, italic=True)
        for day in range(1, days + 1):
            _cell(row[2 + day], item.days.get(day) or "", size=10, italic=True)
        _cell(row[total_col], item.total_hours or "", size=10, italic=True)
        _cell(row[total_col + 1], item.excused_hours or "", size=10, italic=True)
        _cell(row[total_col + 2], item.absent_hours or "", size=10, italic=True)

    if not report.students:
        row = _new_row(table, widths, height=0.6)
        _cell(row[0], "—", size=8)
        _cell(row[1], "В группе пока нет студентов", size=9, align=WD_ALIGN_PARAGRAPH.LEFT)
        for column in range(2, cols):
            _cell(row[column], "", size=7)

    _paragraph(document, "", size=8)
    _documents_table(document, report)
    _paragraph(document, "", size=8)
    _signatures(document)
    _paid_work_table(document, report)
    _signatures(document)

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Ведомость платных отработок по дисциплине
# --------------------------------------------------------------------------- #

# Ширины колонок из бланка вуза (в twips), 13 колонок.
PAID_BASE_WIDTHS = (
    933, 855, 1343, 1009, 263, 576, 697, 94, 225, 1056, 965, 514, 1961,
)


def _paid_layout(document) -> list[float]:
    content = _content_width(document.sections[0])
    widths = [value * CM_PER_TWIP for value in PAID_BASE_WIDTHS]
    scale = content / sum(widths)
    return [width * scale for width in widths]


def _paid_header(table, group: PaidGroup, month_name: str, code: str) -> None:
    """Шапка бланка: 6 строк с объединёнными ячейками."""

    def merge(row: int, start: int, end: int, text, **kwargs) -> None:
        cell = table.cell(row, start)
        if end > start:
            cell = cell.merge(table.cell(row, end))
        _cell(cell, text, font="Calibri", **kwargs)

    merge(0, 0, 5, group.subject, size=12)
    merge(0, 6, 6, "", size=12)
    merge(0, 7, 12, group.teacher, size=12)

    merge(1, 0, 5, "название дисциплины", size=9)
    merge(1, 6, 6, "", size=9)
    merge(1, 7, 12, "ФИО преподавателя", size=9)

    merge(2, 0, 1, "за", size=10, align=WD_ALIGN_PARAGRAPH.LEFT)
    merge(2, 2, 3, month_name, size=10)
    merge(2, 4, 8, "группы", size=10)
    merge(2, 9, 10, code, size=10)
    merge(2, 11, 12, "", size=10)

    merge(3, 0, 1, "", size=9)
    merge(3, 2, 3, "месяц", size=9)
    for start, end in ((4, 8), (9, 10), (11, 12)):
        merge(3, start, end, "", size=9)

    merge(4, 0, 0, "№ п/п", bold=True, size=11)
    merge(4, 1, 2, "ФИО студента полностью", bold=True, size=11)
    merge(4, 3, 4, "Дата пропуска занятия", bold=True, size=11)
    merge(4, 5, 7, "Сумма к оплате*, руб", bold=True, size=11)
    merge(4, 8, 9, "Номер операции в ЕРИП", bold=True, size=11)
    merge(4, 10, 11, "Дата совершения оплаты", bold=True, size=11)
    merge(4, 12, 12, "Подпись преподавателя об отработке/дата сдачи", bold=True, size=11)

    merge(5, 0, 0, 1, bold=True, size=11)
    merge(5, 1, 2, 2, bold=True, size=11)
    merge(5, 3, 4, 3, bold=True, size=11)
    merge(5, 5, 7, 4, bold=True, size=11)
    merge(5, 8, 9, 5, bold=True, size=11)
    merge(5, 10, 11, 6, bold=True, size=11)
    merge(5, 12, 12, 7, bold=True, size=11)


def render_paid(report: PaidReport) -> bytes:
    document = _new_document(margins=(3.0, 1.5, 2.0, 2.0), base_size=10)
    widths = _paid_layout(document)

    if not report.groups:
        _paragraph(
            document,
            "Ведомость учета платных отработок по дисциплине",
            size=11,
            space_after=6,
        )
        _paragraph(
            document,
            f"за {report.month_name} {report.year} г. группы {report.group.code}",
            size=10,
            space_after=6,
        )
        _paragraph(
            document,
            "Пропусков занятий без уважительной причины за месяц нет.",
            size=10,
            align=WD_ALIGN_PARAGRAPH.LEFT,
        )
        buffer = BytesIO()
        document.save(buffer)
        return buffer.getvalue()

    for index, group in enumerate(report.groups):
        if index:
            document.add_page_break()

        _paragraph(
            document,
            "Ведомость учета платных отработок по дисциплине",
            size=11,
            space_after=6,
        )

        # Пустые строки — как в бланке: ведомость заполняют и от руки.
        rows = 6 + max(20, len(group.entries))
        table = _table(document, rows=rows, cols=13, widths=widths)
        _paid_header(table, group, report.month_name, report.group.code)
        for row in table.rows[:6]:
            _keep_row(row)
            row.height = Cm(0.5)

        for index in range(rows - 6):
            entry = group.entries[index] if index < len(group.entries) else None
            cells = table.rows[6 + index].cells
            _cell(cells[0], f"{index + 1}." if entry else "", size=10)
            merged = cells[1].merge(cells[2])
            _cell(
                merged,
                entry.member.full_name if entry else "",
                size=10,
                align=WD_ALIGN_PARAGRAPH.LEFT,
            )
            merged = cells[3].merge(cells[4])
            _cell(merged, format_date_short(entry.day) if entry else "", size=10)
            merged = cells[5].merge(cells[7])
            _cell(merged, _money(entry.amount) if entry else "", size=10)
            merged = cells[8].merge(cells[9])
            _cell(merged, "", size=10)
            merged = cells[10].merge(cells[11])
            _cell(merged, "", size=10)
            _cell(cells[12], "", size=10)

        _caption(
            document,
            "*Если занятие пропущено по уважительной причине, "
            "делается отметка «без оплаты»",
        )

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()
