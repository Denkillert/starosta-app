"""Собирает DOCX-шаблоны ведомостей из образцов, которые ведёт университет.

Образцы лежат вне репозитория (по умолчанию ``D:\\Универ``). Скрипт нужно
запускать только когда меняются сами бланки: он один раз «вычищает» образцы —
убирает реальные фамилии и отметки, ставит подстановки вида ``{{GROUP}}`` — и
кладёт результат в ``app/resources/docx``. Дальше печать строится по шаблону,
поэтому шрифты, размеры, объединения ячеек и разрывы страниц совпадают с
бланком один в один.

Запуск::

    .venv\\Scripts\\python.exe tools\\build_docx_templates.py [каталог-с-образцами]
"""
from __future__ import annotations

import sys
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Twips
from docx.table import _Cell

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "app" / "resources" / "docx"

PROPUSKI_SRC = "Ведомость пропусков сентябрь.docx"
PAID_SRC = "Ведомость учета платных отработок по дисциплине (Октябрь).docx"

# Сколько пустых строк оставляем в бланках, когда данных меньше.
DOCS_MIN_ROWS = 13
PAID_WORK_MIN_ROWS = 33
PAID_DISCIPLINE_ROWS = 20


def _rows(table) -> list:
    return table._tbl.findall(qn("w:tr"))


def _cells(tr, table) -> list:
    return [_Cell(tc, table) for tc in tr.findall(qn("w:tc"))]


def clear_cell(cell) -> None:
    """Оставляет форматирование, но убирает текст."""
    paragraphs = cell.paragraphs
    for extra in paragraphs[1:]:
        extra._p.getparent().remove(extra._p)
    runs = paragraphs[0].runs
    if runs:
        runs[0].text = ""
        for run in runs[1:]:
            run._r.getparent().remove(run._r)


def keep_rows(table, count: int) -> None:
    """Удаляет все строки, кроме первых ``count``."""
    for tr in _rows(table)[count:]:
        table._tbl.remove(tr)


def set_row_height(tr, twips: int) -> None:
    """Высота строки-образца: в заполненном бланке она «раздута» содержимым."""
    tr_pr = tr.find(qn("w:trPr"))
    if tr_pr is None:
        return
    height = tr_pr.find(qn("w:trHeight"))
    if height is None:
        return
    height.set(qn("w:val"), str(twips))


def allow_name_overflow(cell, twips: int = -560) -> None:
    """Разрешает фамилии не переноситься: узкая колонка ФИО в бланке.

    В образце такие отступы проставлены вручную только там, где фамилия не
    влезала. Мы ставим один общий: текст просто занимает соседнюю пустую
    колонку отметок, как и в заполненном бланке.
    """
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.right_indent = Twips(twips)


def build_propuski(source: Path) -> Path:
    doc = Document(str(source))
    paragraphs = doc.paragraphs
    assert len(paragraphs) == 21, f"неожиданное число абзацев: {len(paragraphs)}"

    month_line = paragraphs[2]
    assert month_line.text == "за сентябрь месяц 2026 г.", month_line.text
    runs = month_line.runs
    runs[2].text = "{{MONTH}}"
    runs[4].text = "месяц {{YEAR}}"
    for run in runs[5:7]:
        run._r.getparent().remove(run._r)

    group_line = paragraphs[3]
    assert "24ИТ-2" in group_line.text, group_line.text
    group_runs = group_line.runs
    assert group_runs[3].text == "2", group_runs[3].text
    group_runs[3].text = "{{COURSE}}"
    for run in group_runs:
        if "24ИТ-2" in run.text:
            run.text = "       {{GROUP}}        "
            break
    else:  # pragma: no cover - защита от изменения образца
        raise AssertionError("в строке «Курс/ГРУППА» не найден код группы")

    main_table = doc.tables[0]
    assert len(_rows(main_table)) == 28, len(_rows(main_table))
    keep_rows(main_table, 3)
    prototype_cells = _cells(_rows(main_table)[2], main_table)
    for cell in prototype_cells:
        clear_cell(cell)
    allow_name_overflow(prototype_cells[1])

    documents = doc.tables[1]
    assert len(_rows(documents)) == 15, len(_rows(documents))
    keep_rows(documents, 3)
    set_row_height(_rows(documents)[2], 266)
    for cell in _cells(_rows(documents)[2], documents):
        clear_cell(cell)

    paid_work = doc.tables[2]
    assert len(_rows(paid_work)) == 34, len(_rows(paid_work))
    keep_rows(paid_work, 2)
    for cell in _cells(_rows(paid_work)[1], paid_work):
        clear_cell(cell)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / "propuski.docx"
    doc.save(str(target))
    return target


def build_paid(source: Path) -> Path:
    doc = Document(str(source))
    body = doc.element.body
    section_properties = body.find(qn("w:sectPr"))
    children = [child for child in body.iterchildren() if child is not section_properties]

    # Оставляем ровно одну страницу: заголовок, таблица, сноска и абзац-разрыв.
    stop = None
    for index, child in enumerate(children):
        if child.tag != qn("w:p"):
            continue
        breaks = [br.get(qn("w:type")) for br in child.findall(".//" + qn("w:br"))]
        if "page" in breaks:
            stop = index
            break
    assert stop is not None, "в образце нет разрыва страницы"
    for child in children[stop + 1 :]:
        body.remove(child)

    table = doc.tables[0]
    assert len(_rows(table)) == 26, len(_rows(table))

    first_row = _cells(_rows(table)[0], table)
    assert len(first_row) == 3, len(first_row)
    first_row[0].paragraphs[0].runs[0].text = "{{SUBJECT}}"
    clear_cell(first_row[1])
    first_row[2].paragraphs[0].runs[0].text = "{{TEACHER}}"

    month_row = _cells(_rows(table)[2], table)
    assert month_row[1].text.strip() == "Октябрь", month_row[1].text
    month_row[1].paragraphs[0].runs[0].text = "{{MONTH}}"
    assert month_row[3].text.strip() == "24ИТ-2", month_row[3].text
    month_row[3].paragraphs[0].runs[0].text = "{{GROUP}}"

    for tr in _rows(table)[6:]:
        for cell in _cells(tr, table):
            clear_cell(cell)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / "paid.docx"
    doc.save(str(target))
    return target


def main(argv: list[str]) -> int:
    source_dir = Path(argv[1]) if len(argv) > 1 else Path(r"D:\Универ")
    for name in (PROPUSKI_SRC, PAID_SRC):
        if not (source_dir / name).exists():
            print(f"нет образца: {source_dir / name}", file=sys.stderr)
            return 1

    print("propuski:", build_propuski(source_dir / PROPUSKI_SRC))
    print("paid:", build_paid(source_dir / PAID_SRC))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
