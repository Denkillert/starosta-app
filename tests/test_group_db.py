from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine, text

from app.config import settings
from app.models import Group, User
from app.security import sign_file_token
from app.services import group_db as group_db_service
from app.services import groups as groups_service


def _query(path, sql: str):
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql)).all()
    finally:
        engine.dispose()


def test_export_contains_only_own_group(db, seeded, tmp_path, monkeypatch):
    monkeypatch.setattr(group_db_service, "EXPORT_DIR", tmp_path)
    actor = db.get(User, seeded["user_id"])

    other, _ = groups_service.create_group(
        db, user=actor, code="24ИТ-9", name="24ИТ-9 (тест)"
    )
    groups_service.add_member(
        db, group=other, full_name="Чужой Студент Посторонний", actor=actor
    )
    db.commit()

    group = db.get(Group, seeded["group_id"])
    path = group_db_service.export_group(db, group)
    assert path.exists()
    assert path.parent == tmp_path

    with sqlite3.connect(path) as conn:
        codes = [row[0] for row in conn.execute("select code from groups")]
        names = [row[0] for row in conn.execute("select full_name from memberships")]
        lessons = conn.execute("select count(*) from lessons").fetchone()[0]
        occurrences = conn.execute(
            "select count(*) from lesson_occurrences"
        ).fetchone()[0]
        assert conn.execute("pragma foreign_key_check").fetchall() == []

    assert codes == [group.code]
    assert "Чужой Студент Посторонний" not in names
    assert len(names) == 1 + 4
    assert lessons > 0 and occurrences > 0


def test_export_rebuilds_file_without_stale_rows(db, seeded, tmp_path, monkeypatch):
    monkeypatch.setattr(group_db_service, "EXPORT_DIR", tmp_path)
    group = db.get(Group, seeded["group_id"])

    first = group_db_service.export_group(db, group)
    groups_service.add_member(
        db, group=group, full_name="Новиков Пётр Сергеевич", actor=None
    )
    db.commit()
    second = group_db_service.export_group(db, group)

    assert first == second
    names = [row[0] for row in _query(second, "select full_name from memberships")]
    assert "Новиков Пётр Сергеевич" in names
    assert len(names) == 1 + 5


def test_group_database_download(client, seeded, tmp_path, monkeypatch):
    monkeypatch.setattr(group_db_service, "EXPORT_DIR", tmp_path)
    monkeypatch.setattr(settings, "dev_mode", False)
    token = sign_file_token(user_id=seeded["user_id"], group_id=seeded["group_id"])

    response = client.get(
        f"/groups/{seeded['group_id']}/database.db",
        params={"group": seeded["group_id"], "token": token},
    )
    assert response.status_code == 200, response.text
    assert response.content[:15] == b"SQLite format 3"
    assert "starosta-" in response.headers["content-disposition"]


def test_group_database_rejects_other_group_token(
    client, seeded, tmp_path, monkeypatch
):
    # Без cookie: права подтверждает только подпись в ссылке.
    monkeypatch.setattr(group_db_service, "EXPORT_DIR", tmp_path)
    monkeypatch.setattr(settings, "dev_mode", False)
    token = sign_file_token(user_id=seeded["user_id"], group_id=seeded["group_id"])

    response = client.get(
        f"/groups/{seeded['group_id'] + 100}/database.db",
        params={"group": seeded["group_id"] + 100, "token": token},
    )
    assert response.status_code == 403
