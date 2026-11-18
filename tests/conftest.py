from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base, get_db
from app.main import app as fastapi_app
from app.models import LessonOccurrence
from app.services import groups as groups_service
from app.services.groups import ensure_user
from app.services.polessu import load_payload_file
from app.services.schedule_sync import import_parsed

# Фоновый цикл не должен запускаться в тестах: он ходит в сеть и мешает
# предсказуемому завершению TestClient.
settings.scheduler_enabled = False

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
STUDENTS = (
    "Иванова Анна Сергеевна",
    "Ковальчук Дмитрий Олегович",
    "Мороз Екатерина Павловна",
    "Шевчук Артём Игоревич",
)


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture()
def client(session_factory):
    def override():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_db] = override
    with TestClient(fastapi_app) as test_client:
        yield test_client
    fastapi_app.dependency_overrides.clear()


@pytest.fixture()
def seeded(session_factory):
    with session_factory() as db:
        user = ensure_user(
            db,
            telegram_id=settings.dev_telegram_id,
            full_name=settings.dev_user_name,
        )
        db.commit()
        group, _head = groups_service.create_group(
            db, user=user, code="24ИТ-2", name="24ИТ-2 (тест)"
        )
        members = [
            groups_service.add_member(db, group=group, full_name=name, actor=user)
            for name in STUDENTS
        ]
        parsed = load_payload_file(ROOT / "app" / "fixtures" / "schedule_24it2_term1.json")
        import_parsed(db, group, 1, parsed, actor=user)
        first_date = db.scalar(
            select(func.min(LessonOccurrence.date)).where(
                LessonOccurrence.group_id == group.id
            )
        )
        return {
            "group_id": group.id,
            "user_id": user.id,
            "members": [m.id for m in members],
            "invite_code": group.invite_code,
            "first_date": first_date.isoformat() if first_date else None,
        }


@pytest.fixture()
def api_client(client, seeded):
    return client
