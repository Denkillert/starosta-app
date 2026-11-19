"""Парсер расписания ПолесГУ.

Источник: https://www.polessu.by/ruz/?f=<term>&q=<group>
Страница отдаётся готовым HTML, таблица ``tbody#weeks-filter``:
день — ``tr.wa``, пары — ``tr.wN``, где ``wN`` — неделя ``N+1``.

Модуль намеренно не тянет bs4/lxml: используется стандартный HTMLParser,
поэтому парсер работает на любой версии Python без нативных колёс.
"""

from __future__ import annotations

import hashlib
import json
import re
import time as time_module
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path

import httpx

from ..config import settings

BASE_URL = "https://www.polessu.by"
WEEKDAYS: dict[str, int] = {
    "Понедельник": 0,
    "Вторник": 1,
    "Среда": 2,
    "Четверг": 3,
    "Пятница": 4,
    "Суббота": 5,
    "Воскресенье": 6,
}


class ScheduleParseError(RuntimeError):
    """Страница скачалась, но распознать расписание не удалось."""


class ScheduleFetchError(RuntimeError):
    """Не удалось скачать страницу расписания."""


@dataclass(slots=True)
class ParsedLesson:
    day: str
    day_of_week: int
    start_time: time
    end_time: time
    subject: str
    kind: str
    room: str
    teacher: str
    subgroup: str | None
    weeks: list[int]
    dates: list[date] = field(default_factory=list)

    @property
    def source_key(self) -> str:
        raw = "|".join(
            [
                str(self.day_of_week),
                self.start_time.strftime("%H:%M"),
                self.end_time.strftime("%H:%M"),
                self.subject,
                self.kind,
                self.room,
                self.teacher,
                self.subgroup or "",
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ParsedWeek:
    number: int
    start: date
    end: date

    def to_dict(self) -> dict:
        return {"number": self.number, "start": self.start.isoformat(), "end": self.end.isoformat()}


@dataclass(slots=True)
class ParsedSchedule:
    group: str
    term: int
    source_url: str
    fetched_at: datetime
    weeks: list[ParsedWeek]
    lessons: list[ParsedLesson]
    meta: dict
    content_hash: str = ""

    @property
    def lesson_count(self) -> int:
        return len(self.lessons)

    @property
    def week_count(self) -> int:
        return len(self.weeks)

    def to_payload(self) -> dict:
        return {
            "group": self.group,
            "term": self.term,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at.isoformat(timespec="seconds"),
            "meta": self.meta,
            "weeks": [w.to_dict() for w in self.weeks],
            "lessons": [
                {
                    "day": lesson.day,
                    "day_of_week": lesson.day_of_week,
                    "start": lesson.start_time.strftime("%H:%M"),
                    "end": lesson.end_time.strftime("%H:%M"),
                    "subject": lesson.subject,
                    "kind": lesson.kind,
                    "room": lesson.room,
                    "teacher": lesson.teacher,
                    "subgroup": lesson.subgroup,
                    "weeks": lesson.weeks,
                    "dates": [d.isoformat() for d in lesson.dates],
                }
                for lesson in self.lessons
            ],
            "lessons_total": len(self.lessons),
        }

    @classmethod
    def from_payload(cls, payload: dict) -> ParsedSchedule:
        weeks = [
            ParsedWeek(
                number=int(w["number"]),
                start=date.fromisoformat(w["start"]),
                end=date.fromisoformat(w["end"]),
            )
            for w in payload.get("weeks", [])
        ]
        lessons = [
            ParsedLesson(
                day=item["day"],
                day_of_week=int(item["day_of_week"]),
                start_time=time.fromisoformat(item["start"]),
                end_time=time.fromisoformat(item["end"]),
                subject=item["subject"],
                kind=item.get("kind", ""),
                room=item.get("room", ""),
                teacher=item.get("teacher", ""),
                subgroup=item.get("subgroup"),
                weeks=[int(v) for v in item.get("weeks", [])],
                dates=[date.fromisoformat(d) for d in item.get("dates", [])],
            )
            for item in payload.get("lessons", [])
        ]
        fetched_raw = payload.get("fetched_at")
        try:
            fetched_at = datetime.fromisoformat(fetched_raw) if fetched_raw else datetime.now()
        except ValueError:
            fetched_at = datetime.now()
        return cls(
            group=payload.get("group", ""),
            term=int(payload.get("term", 1)),
            source_url=payload.get("source_url", ""),
            fetched_at=fetched_at,
            weeks=weeks,
            lessons=lessons,
            meta=payload.get("meta", {}),
            content_hash=payload.get("content_hash", ""),
        )


# --------------------------------------------------------------------------- #
# HTML -> строки таблицы
# --------------------------------------------------------------------------- #
class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._row: dict | None = None
        self._cell: list[str] | None = None
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "tr":
            self._row = {"class": dict(attrs).get("class") or "", "cells": []}
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row["cells"].append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row["cells"]:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._skip or self._cell is None:
            return
        self._cell.append(data)


KIND_RE = re.compile(r"\s*-\s*(лек|лаб|пр|сем|зач|экз|конс|фак|кр|диф)\w*\.?\s*$", re.IGNORECASE)
WEEK_CLASS_RE = re.compile(r"\bw(\d+)\b")
WEEK_MENU_RE = re.compile(
    r'<a href="#w\d+">\s*Неделя\s+(\d+)\s*\(([^)]*)\)\s*</a>', re.IGNORECASE
)
WEEK_LABEL_RE = re.compile(
    r"(\d{1,2})\.(\d{1,2})\.?\s*[-\u2013]\s*(\d{1,2})\.(\d{1,2})\.?"
)
UPDATED_RE = re.compile(
    r"Последние изменения вносились:\s*(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{2}:\d{2}))?"
)
SEMESTER_RE = re.compile(r"На семестр\s*\(([^)]*)\)")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
PREFIX_WEEKS_RE = re.compile(r"^\(([^)]*)\)\s*(.*)$")


def _expand_weeks(spec: str) -> list[int]:
    out: set[int] = set()
    for part in re.split(r"[,;]", spec):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*[-\u2013]\s*(\d+)", part)
        if m:
            out.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            out.add(int(part))
    return sorted(out)


def _parse_time_range(text: str) -> tuple[time, time] | None:
    found = TIME_RE.findall(text)
    if not found:
        return None
    try:
        start = time(int(found[0][0]), int(found[0][1]))
        if len(found) > 1:
            end = time(int(found[1][0]), int(found[1][1]))
        else:
            end = (datetime.combine(date.today(), start) + timedelta(minutes=90)).time()
    except ValueError:
        return None
    if end <= start:
        end = (datetime.combine(date.today(), start) + timedelta(minutes=90)).time()
    return start, end


def _base_year(meta: dict, term: int) -> int:
    updated_year = meta.get("updated_year")
    updated_month = meta.get("updated_month")
    if updated_year and updated_month:
        year, month = int(updated_year), int(updated_month)
    else:
        today = date.today()
        year, month = today.year, today.month
    if term == 1:
        return year if month >= 8 else year - 1
    return year + 1 if month >= 8 else year


def _parse_meta(html: str) -> dict:
    updated = UPDATED_RE.search(html)
    semester = SEMESTER_RE.search(html)
    week_menu = [
        {"number": int(number), "range": label.strip()}
        for number, label in WEEK_MENU_RE.findall(html)
    ]
    meta: dict = {
        "updated": None,
        "updated_year": None,
        "updated_month": None,
        "semester_range": semester.group(1).strip() if semester else None,
        "week_menu": week_menu,
    }
    if updated:
        day, month, year, hhmm = updated.groups()
        meta["updated"] = (
            f"{day}.{month}.{year}" + (f" {hhmm}" if hhmm else "")
        )
        meta["updated_year"] = int(year)
        meta["updated_month"] = int(month)
    return meta


def _build_weeks(meta: dict, term: int) -> list[ParsedWeek]:
    base_year = _base_year(meta, term)
    weeks: list[ParsedWeek] = []
    for item in meta.get("week_menu", []):
        m = WEEK_LABEL_RE.search(item["range"])
        if not m:
            continue
        start_day, start_month, end_day, end_month = (int(x) for x in m.groups())
        try:
            start = date(base_year, start_month, start_day)
            end_year = base_year + 1 if end_month < start_month else base_year
            end = date(end_year, end_month, end_day)
        except ValueError:
            continue
        if end < start:
            end = date(end.year + 1, end.month, end.day)
        weeks.append(ParsedWeek(number=item["number"], start=start, end=end))
    weeks.sort(key=lambda w: w.number)
    return weeks


def _attach_dates(lessons: list[ParsedLesson], weeks: list[ParsedWeek]) -> None:
    if not weeks:
        return
    by_number = {w.number: w for w in weeks}
    # Сайт нумерует учебные недели со вторника по воскресенье, поэтому
    # понедельник относится к той же календарной неделе, что и её начало.
    semester_start = min(w.start for w in weeks)
    for lesson in lessons:
        seen: set[date] = set()
        for number in lesson.weeks:
            week = by_number.get(number)
            if week is None:
                continue
            week_monday = week.start - timedelta(days=week.start.weekday())
            day = week_monday + timedelta(days=lesson.day_of_week)
            if semester_start <= day <= week.end and day not in seen:
                seen.add(day)
        lesson.dates = sorted(seen)


def parse_schedule(
    html: str, *, group: str, term: int, source_url: str = ""
) -> ParsedSchedule:
    parser = _TableParser()
    parser.feed(html)
    parser.close()

    lessons: list[ParsedLesson] = []
    current_day = ""
    skipped = 0

    for row in parser.rows:
        cells = row["cells"]
        classes = row["class"]

        if "wa" in classes.split():
            if cells:
                current_day = cells[0].strip()
            continue
        if len(cells) < 4 or not current_day:
            continue

        time_text, subject_text, room, teacher = cells[0], cells[1], cells[2], cells[3]
        subgroup = cells[4].strip() if len(cells) > 4 and cells[4].strip() else None

        time_range = _parse_time_range(time_text)
        if time_range is None:
            skipped += 1
            continue
        start_time, end_time = time_range

        prefix_weeks: list[int] = []
        m = PREFIX_WEEKS_RE.match(subject_text)
        if m:
            prefix_weeks = _expand_weeks(m.group(1))
            subject_text = m.group(2)

        kind = ""
        km = KIND_RE.search(subject_text)
        if km:
            kind = subject_text[km.start() :].strip(" -\u2013")
            subject_text = subject_text[: km.start()]
        subject_text = subject_text.strip(" -\u2013")

        weeks = sorted({int(x) + 1 for x in WEEK_CLASS_RE.findall(classes)})
        if not weeks:
            weeks = prefix_weeks

        if not subject_text:
            skipped += 1
            continue
        if current_day not in WEEKDAYS:
            skipped += 1
            continue

        lessons.append(
            ParsedLesson(
                day=current_day,
                day_of_week=WEEKDAYS[current_day],
                start_time=start_time,
                end_time=end_time,
                subject=subject_text,
                kind=kind,
                room=room.strip(),
                teacher=teacher.strip(),
                subgroup=subgroup,
                weeks=weeks,
            )
        )

    if not lessons:
        raise ScheduleParseError(
            "В ответе нет ни одной пары. Возможно, группа указана неверно "
            "или изменилась вёрстка сайта."
        )

    meta = _parse_meta(html)
    meta["skipped_rows"] = skipped
    weeks = _build_weeks(meta, term)
    _attach_dates(lessons, weeks)
    lessons.sort(
        key=lambda x: (x.day_of_week, x.start_time, x.subject, x.subgroup or "")
    )

    payload = {
        "group": group,
        "term": term,
        "weeks": [w.to_dict() for w in weeks],
        "lessons": [
            [
                l.day_of_week,
                l.start_time.strftime("%H:%M"),
                l.end_time.strftime("%H:%M"),
                l.subject,
                l.kind,
                l.room,
                l.teacher,
                l.subgroup or "",
                l.weeks,
            ]
            for l in lessons
        ],
    }
    content_hash = hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    return ParsedSchedule(
        group=group,
        term=term,
        source_url=source_url or schedule_url(group, term),
        fetched_at=datetime.now(),
        weeks=weeks,
        lessons=lessons,
        meta=meta,
        content_hash=content_hash,
    )


def schedule_url(group: str, term: int) -> str:
    return f"{BASE_URL}/ruz/?f={term}&q={urllib.parse.quote(group)}"


def fetch_html(url: str, *, timeout: float = 25.0, retries: int = 3) -> str:
    last_error: Exception | None = None
    headers = {
        "User-Agent": settings.parser_user_agent,
        "Accept-Language": "ru,en;q=0.8",
    }
    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                return response.text
        except Exception as exc:  # noqa: BLE001 - повторяем запрос и показываем последнюю ошибку
            last_error = exc
            if attempt < retries:
                time_module.sleep(1.5 * attempt)
    raise ScheduleFetchError(f"Не удалось загрузить {url}: {last_error}")


def fetch_and_parse(group: str, term: int, *, fetcher=fetch_html) -> ParsedSchedule:
    url = schedule_url(group, term)
    html = fetcher(url)
    return parse_schedule(html, group=group, term=term, source_url=url)


def load_payload_file(path) -> ParsedSchedule:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    schedule = ParsedSchedule.from_payload(data)
    if not schedule.group:
        schedule.group = data.get("group", "")
    return schedule
