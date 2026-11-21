"""Работа с DOCX-шаблонами ведомостей.

Бланки вуза живут в ``app/resources/docx`` и собираются из настоящих образцов
скриптом ``tools/build_docx_templates.py``. Поэтому печать повторяет бланк
один в один: те же шрифты, размеры, объединения ячеек и разрывы страниц.
"""
from __future__ import annotations

import copy
from io import BytesIO
from pathlib import Path

from docx import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.table import _Cell
from docx.text.paragraph import Paragraph

RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources" / "docx"


def open_template(name: str) -> DocxDocument:
    """Открывает бланк из ресурсов (копия в памяти, файл не меняется)."""
    return DocxDocument(BytesIO((RESOURCES_DIR / name).read_bytes()))


def iter_paragraphs(document) -> list[Paragraph]:
    """Все абзацы документа, включая ячейки таблиц."""
    return [Paragraph(element, document) for element in document.element.body.iter(qn("w:p"))]


def replace_tokens(document, mapping: dict[str, object]) -> None:
    """Подставляет ``{{КЛЮЧ}}`` в текстовые прогоны, сохраняя оформление."""
    for paragraph in iter_paragraphs(document):
        for run in paragraph.runs:
            if "{{" not in run.text:
                continue
            text = run.text
            for key, value in mapping.items():
                text = text.replace("{{" + key + "}}", str(value))
            run.text = text


def table_rows(table) -> list:
    """Строки таблицы как XML-элементы (в отличие от ``table.rows``)."""
    return table._tbl.findall(qn("w:tr"))


def row_cells(table, tr) -> list[_Cell]:
    """Ячейки строки по порядку — без размножения объединённых."""
    return [_Cell(tc, table) for tc in tr.findall(qn("w:tc"))]


def drop_rows(table, start: int) -> None:
    """Удаляет строки начиная с индекса ``start`` (шаблон для клонирования)."""
    for tr in table_rows(table)[start:]:
        table._tbl.remove(tr)


def append_row(table, tr) -> object:
    """Добавляет копию строки в конец таблицы и возвращает новый элемент."""
    clone = copy.deepcopy(tr)
    table._tbl.append(clone)
    return clone


def _rpr(cell: _Cell | None):
    if cell is None:
        return None
    for paragraph in cell.paragraphs:
        for run in paragraph.runs:
            found = run._r.find(qn("w:rPr"))
            if found is not None:
                return found
    return None


def set_cell(cell: _Cell, value: object, *, donor: _Cell | None = None) -> None:
    """Пишет значение в ячейку, сохраняя шрифт.

    Оформление берётся из первого прогона самой ячейки; если прогонов нет
    (пустая строка бланка), копируется оформление из ячейки ``donor``.
    """
    paragraphs = cell.paragraphs
    for extra in paragraphs[1:]:
        extra._p.getparent().remove(extra._p)

    paragraph = paragraphs[0]
    text = "" if value is None else str(value)
    runs = paragraph.runs
    if runs:
        runs[0].text = text
        for run in runs[1:]:
            run._r.getparent().remove(run._r)
        return
    if not text:
        return

    run = paragraph.add_run(text)
    template_rpr = _rpr(donor)
    if template_rpr is not None:
        run._r.insert(0, copy.deepcopy(template_rpr))


def center_cell(cell: _Cell) -> None:
    """Выравнивает все абзацы ячейки по центру (даты в колонках)."""
    for paragraph in cell.paragraphs:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
