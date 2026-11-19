from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

connect_args: dict = {}
engine_kwargs: dict = {"pool_pre_ping": True, "future": True}

if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
else:
    engine_kwargs["pool_recycle"] = 3600

engine = create_engine(settings.database_url, connect_args=connect_args, **engine_kwargs)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_fk(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Простые добавления колонок для уже существующих баз: Alembic в проекте нет,
# а `create_all` не меняет готовые таблицы. Для новых баз этот шаг пустой.
_COMPAT_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "memberships": (
        ("funding", "VARCHAR(16) NOT NULL DEFAULT 'budget'"),
        ("subgroup", "INTEGER NULL"),
        ("needs_review", "BOOLEAN NOT NULL DEFAULT 0"),
    ),
    "documents": (
        ("valid_from_time", "TIME NULL"),
        ("valid_to_time", "TIME NULL"),
    ),
    "attendance": (("previous_status", "VARCHAR(16) NULL"),),
}


def _add_missing_columns() -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, columns in _COMPAT_COLUMNS.items():
            if table not in tables:
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, ddl in columns:
                if name not in existing:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    )


def _migrate_document_types() -> None:
    """Убирает устаревший тип «разрешение»: остаются заявление, распоряжение, справка."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE documents SET type = 'certificate', title = 'Справка' "
                "WHERE type = 'permission'"
            )
        )


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
    _add_missing_columns()
    _migrate_document_types()
