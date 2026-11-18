from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import settings

TZ = ZoneInfo(settings.timezone)

RU_MONTHS_GEN = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
RU_WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
RU_WEEKDAYS_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def now_local() -> datetime:
    return datetime.now(TZ)


def today_local() -> date:
    return now_local().date()


def format_date(day: date | None, *, with_weekday: bool = False) -> str:
    if day is None:
        return "—"
    text = f"{day.day} {RU_MONTHS_GEN[day.month - 1]} {day.year}"
    if with_weekday:
        text = f"{RU_WEEKDAYS_SHORT[day.weekday()]}, {text}"
    return text


def format_date_short(day: date | None) -> str:
    if day is None:
        return "—"
    return f"{day.day:02d}.{day.month:02d}.{day.year}"


def format_period(start: date | None, end: date | None) -> str:
    if start and end:
        if start == end:
            return format_date(start)
        return f"{format_date(start)} — {format_date(end)}"
    if start:
        return f"с {format_date(start)}"
    if end:
        return f"до {format_date(end)}"
    return "—"


def format_time(value) -> str:
    return value.strftime("%H:%M") if value else "—"


def short_name(full_name: str | None) -> str:
    """«Иванова Анна Сергеевна» → «Иванова А.С.» для журнала и ведомости."""
    if not full_name:
        return "—"
    parts = [part for part in full_name.replace(".", ". ").split() if part]
    if not parts:
        return "—"
    if len(parts) == 1:
        return parts[0]
    initials = "".join(f"{part[0]}." for part in parts[1:] if part)
    return f"{parts[0]} {initials}".strip()


def course_for_code(code: str | None, on: date | None = None) -> int | None:
    """Курс по коду группы вида «24ИТ-2» на текущий учебный год."""
    if not code or len(code) < 2 or not code[:2].isdigit():
        return None
    admission = 2000 + int(code[:2])
    day = on or today_local()
    academic_start = day.year if day.month >= 9 else day.year - 1
    course = academic_start - admission + 1
    return course if 1 <= course <= 6 else None


def parse_int(value, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_date(value: str | None, default: date | None = None) -> date | None:
    if not value:
        return default
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return default
