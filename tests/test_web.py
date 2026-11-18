from __future__ import annotations

from pathlib import Path


def test_dashboard_renders(api_client):
    response = api_client.get("/")
    assert response.status_code == 200, response.text
    assert "Староста" in response.text
    assert "Пары сегодня" in response.text


def test_schedule_page(api_client):
    response = api_client.get("/schedule")
    assert response.status_code == 200, response.text
    assert "Расписание" in response.text


def test_journal_page(api_client, seeded):
    response = api_client.get("/journal", params={"date": seeded["first_date"]})
    assert response.status_code == 200, response.text
    assert "Журнал" in response.text
    assert "Снять отметки за день" in response.text


def test_students_page_has_invite_code(api_client, seeded):
    response = api_client.get("/students")
    assert response.status_code == 200, response.text
    assert seeded["invite_code"] in response.text


def test_documents_page_and_form(api_client):
    listing = api_client.get("/documents")
    assert listing.status_code == 200, listing.text
    assert "Документы" in listing.text

    form = api_client.get("/documents/new", params={"type": "certificate"})
    assert form.status_code == 200, form.text
    assert "Справка" in form.text
    assert "Разрешение" not in form.text
    assert "Студент" in form.text


def test_web_attendance_form(api_client, seeded):
    page = api_client.get("/journal", params={"date": seeded["first_date"]})
    assert page.status_code == 200


    from app.db import SessionLocal  # noqa: F401  (не используется, но проверяет импорт)

    # Берём пару через API, чтобы не зависеть от порядка отрисовки.
    schedule = api_client.get(
        f"/api/groups/{seeded['group_id']}/schedule",
        params={"date": seeded["first_date"], "days": 1},
    ).json()
    occurrence = schedule["days"][0]["occurrences"][0]
    member_id = seeded["members"][0]

    response = api_client.post(
        "/actions/attendance/toggle",
        data={
            "occurrence_id": occurrence["id"],
            "member_id": member_id,
            "next": f"/journal?group={seeded['group_id']}&date={seeded['first_date']}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "msg" in response.headers["location"]


def test_reports_page_has_month_vedomosti(api_client):
    response = api_client.get("/reports")
    assert response.status_code == 200, response.text
    assert "Ведомость учета учебных часов" in response.text
    assert "Ведомость платных отработок" in response.text
    assert "/reports/propuski.docx" in response.text
    assert "/reports/paid.docx" in response.text


def test_reports_docx_download(api_client):
    response = api_client.get("/reports/propuski.docx")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"
    assert "wordprocessingml" in response.headers["content-type"]

    response = api_client.get("/reports/paid.docx")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"


def test_stats_csv(api_client):
    response = api_client.get("/stats.csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "Студент" in response.text


def test_404_page(api_client):
    response = api_client.get("/documents/999999")
    assert response.status_code == 404
    assert "не найдена" in response.text.lower()


def test_demo_seed_flow(client):
    response = client.post("/actions/demo", follow_redirects=True)
    assert response.status_code == 200, response.text
    assert "24ИТ-2" in response.text
    assert "Пары сегодня" in response.text


def test_attendance_action_targets_group_from_form(api_client, seeded, session_factory):
    """У пользователя две группы: действие должно применяться к выбранной в форме."""
    from app.db import get_db  # noqa: F401
    from app.models import User
    from app.services import groups as groups_service
    from app.services.polessu import load_payload_file
    from app.services.schedule_sync import import_parsed

    root = Path(__file__).resolve().parent.parent

    with session_factory() as db:
        user = db.get(User, seeded["user_id"])
        second, _membership = groups_service.create_group(
            db, user=user, code="24ИТ-1", name="24ИТ-1 (тест)"
        )
        student = groups_service.add_member(
            db, group=second, full_name="Петров Пётр Петрович", actor=user
        )
        parsed = load_payload_file(
            root / "app" / "fixtures" / "schedule_24it2_term1.json"
        )
        import_parsed(db, second, 1, parsed, actor=user)
        second_id = second.id
        student_id = student.id

    schedule = api_client.get(
        f"/api/groups/{second_id}/schedule",
        params={"date": seeded["first_date"], "days": 1},
    ).json()
    occurrence = schedule["days"][0]["occurrences"][0]

    response = api_client.post(
        "/actions/attendance/toggle",
        data={
            "group": str(second_id),
            "occurrence_id": occurrence["id"],
            "member_id": student_id,
            "next": f"/journal?group={second_id}&date={seeded['first_date']}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" not in response.headers["location"]

    journal = api_client.get(
        f"/api/groups/{second_id}/journal", params={"date": seeded["first_date"]}
    ).json()
    target = next(
        o for o in journal["occurrences"] if o["occurrence_id"] == occurrence["id"]
    )
    entry = next(e for e in target["entries"] if e["member_id"] == student_id)
    assert entry["status"] == "absent"
