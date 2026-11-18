from __future__ import annotations

from pathlib import Path

import pytest

from app.services.polessu import (
    ScheduleParseError,
    parse_schedule,
    schedule_url,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(term: int) -> str:
    return (FIXTURES / f"ruz_24it2_term{term}.html").read_text(encoding="utf-8")


def test_schedule_url_encodes_group():
    url = schedule_url("24ИТ-2", 1)
    assert url.startswith("https://www.polessu.by/ruz/?f=1&q=")
    assert "24%D0%98%D0%A2-2" in url


def test_term1_counts_and_dates():
    parsed = parse_schedule(
        load(1), group="24ИТ-2", term=1, source_url="https://example.test"
    )
    assert parsed.lesson_count == 324
    assert parsed.week_count == 16
    assert parsed.weeks[0].start.isoformat() == "2026-08-31"
    assert parsed.weeks[-1].end.isoformat() == "2026-12-19"
    assert all(lesson.dates for lesson in parsed.lessons)
    assert {lesson.day_of_week for lesson in parsed.lessons} == {0, 1, 2, 3, 4}

    first = parsed.lessons[0]
    assert first.subject
    assert first.kind in ("лек.", "лаб.", "пр.", "сем.", "")
    assert first.start_time < first.end_time

    dates = sorted(d for lesson in parsed.lessons for d in lesson.dates)
    assert dates[0].isoformat() >= "2026-08-31"
    assert dates[-1].isoformat() <= "2026-12-19"


def test_term2_has_saturday_and_tuesday_start():
    parsed = parse_schedule(load(2), group="24ИТ-2", term=2)
    assert parsed.lesson_count == 329
    assert parsed.week_count == 20
    assert parsed.weeks[0].start.isoformat() == "2027-02-09"
    assert 5 in {lesson.day_of_week for lesson in parsed.lessons}

    # Первая неделя начинается со вторника: понедельник 08.02 вне семестра,
    # поэтому ровно одна пара остаётся без дат — это известно и допустимо.
    without_dates = [lesson for lesson in parsed.lessons if not lesson.dates]
    assert len(without_dates) == 1
    assert without_dates[0].day == "Понедельник"


def test_content_hash_is_stable():
    first = parse_schedule(load(1), group="24ИТ-2", term=1)
    second = parse_schedule(load(1), group="24ИТ-2", term=1)
    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 40


def test_payload_roundtrip():
    parsed = parse_schedule(load(1), group="24ИТ-2", term=1)
    payload = parsed.to_payload()
    assert payload["lessons_total"] == 324
    assert len(payload["weeks"]) == 16
    assert payload["lessons"][0]["dates"]

    from app.services.polessu import ParsedSchedule

    restored = ParsedSchedule.from_payload(payload)
    assert restored.lesson_count == parsed.lesson_count
    assert restored.weeks[0].start == parsed.weeks[0].start
    assert restored.lessons[0].start_time == parsed.lessons[0].start_time


def test_fetched_at_is_naive_but_meta_has_update_marker():
    parsed = parse_schedule(load(1), group="24ИТ-2", term=1)
    assert "12.11.2026" in (parsed.meta["updated"] or "")
    assert parsed.meta["updated_year"] == 2026


def test_empty_page_raises():
    with pytest.raises(ScheduleParseError):
        parse_schedule("<html><body>нет таблицы</body></html>", group="X", term=1)
