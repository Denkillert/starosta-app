"""Скачивает страницы расписания и сохраняет их как фикстуры для тестов и демо.

Запуск (из корня starosta-app):
    python -m tools.fetch_fixtures
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.services.polessu import fetch_html, parse_schedule, schedule_url

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests" / "fixtures"
DEMO = ROOT / "app" / "fixtures"


def main() -> None:
    TESTS.mkdir(parents=True, exist_ok=True)
    DEMO.mkdir(parents=True, exist_ok=True)

    for term in (1, 2):
        url = schedule_url("24ИТ-2", term)
        html = fetch_html(url)
        (TESTS / f"ruz_24it2_term{term}.html").write_text(html, encoding="utf-8")

        schedule = parse_schedule(html, group="24ИТ-2", term=term, source_url=url)
        payload = schedule.to_payload()
        payload["content_hash"] = schedule.content_hash
        (DEMO / f"schedule_24it2_term{term}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"term {term}: {schedule.lesson_count} lessons, "
            f"{schedule.week_count} weeks, updated={schedule.meta.get('updated')}"
        )
        time.sleep(1)


if __name__ == "__main__":
    main()
