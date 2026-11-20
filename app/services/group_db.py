"""Отдельный файл SQLite с данными одной группы.

Рабочая база приложения остаётся общей, но для каждой группы собирается
самостоятельный файл ``data/groups/<код>.db``: его можно скачать, отдать
старосте другой группы или положить в бэкап, не выгружая чужие данные.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from sqlalchemy import Table, create_engine, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from ..config import BASE_DIR
from ..db import Base
from ..models import (
    Attendance,
    AuditLog,
    Document,
    Group,
    Lesson,
    LessonOccurrence,
    Membership,
    ScheduleSnapshot,
    Subject,
    User,
    document_students,
)

EXPORT_DIR = BASE_DIR / "data" / "groups"

_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def group_db_path(code: str) -> Path:
    """Путь к файлу группы. Код чистим от символов, запрещённых в Windows."""
    safe = _BAD_FILENAME_CHARS.sub("_", (code or "group").strip()) or "group"
    return EXPORT_DIR / f"{safe}.db"


def _select_rows(db: Session, table: Table, condition=None) -> list[dict]:
    stmt = select(table)
    if condition is not None:
        stmt = stmt.where(condition)
    return [dict(row._mapping) for row in db.execute(stmt).all()]


def _copy_group(db: Session, conn: Connection, group: Group) -> None:
    group_id = group.id

    groups = _select_rows(db, Group.__table__, Group.id == group_id)
    memberships = _select_rows(db, Membership.__table__, Membership.group_id == group_id)
    lessons = _select_rows(db, Lesson.__table__, Lesson.group_id == group_id)
    occurrences = _select_rows(
        db, LessonOccurrence.__table__, LessonOccurrence.group_id == group_id
    )
    documents = _select_rows(db, Document.__table__, Document.group_id == group_id)
    snapshots = _select_rows(
        db, ScheduleSnapshot.__table__, ScheduleSnapshot.group_id == group_id
    )
    audits = _select_rows(db, AuditLog.__table__, AuditLog.group_id == group_id)

    occurrence_ids = [row["id"] for row in occurrences]
    attendance = (
        _select_rows(
            db, Attendance.__table__, Attendance.occurrence_id.in_(occurrence_ids)
        )
        if occurrence_ids
        else []
    )

    document_ids = [row["id"] for row in documents]
    doc_links = (
        _select_rows(
            db, document_students, document_students.c.document_id.in_(document_ids)
        )
        if document_ids
        else []
    )

    subject_ids = {row["subject_id"] for row in lessons if row["subject_id"]}
    subjects = (
        _select_rows(db, Subject.__table__, Subject.id.in_(subject_ids))
        if subject_ids
        else []
    )

    # Пользователи нужны только те, кто встречается в данных этой группы.
    user_ids: set[int] = {row["user_id"] for row in memberships if row["user_id"]}
    user_ids |= {row["author_id"] for row in documents if row["author_id"]}
    user_ids |= {row["marked_by_id"] for row in attendance if row["marked_by_id"]}
    user_ids |= {row["actor_id"] for row in audits if row["actor_id"]}
    users = _select_rows(db, User.__table__, User.id.in_(user_ids)) if user_ids else []

    # Порядок важен: сначала родители, потом строки со ссылками на них.
    for table, rows in (
        (User.__table__, users),
        (Subject.__table__, subjects),
        (Group.__table__, groups),
        (Membership.__table__, memberships),
        (Lesson.__table__, lessons),
        (LessonOccurrence.__table__, occurrences),
        (Attendance.__table__, attendance),
        (Document.__table__, documents),
        (document_students, doc_links),
        (ScheduleSnapshot.__table__, snapshots),
        (AuditLog.__table__, audits),
    ):
        if rows:
            conn.execute(table.insert(), rows)


def export_group(db: Session, group: Group) -> Path:
    """Собрать свежий файл группы. Старый файл заменяется атомарно."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = group_db_path(group.code)

    handle, tmp_name = tempfile.mkstemp(
        prefix=".export-", suffix=".db.tmp", dir=EXPORT_DIR
    )
    os.close(handle)
    tmp_path = Path(tmp_name)

    engine = create_engine(f"sqlite:///{tmp_path.as_posix()}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            _copy_group(db, conn, group)
            broken = conn.exec_driver_sql("PRAGMA foreign_key_check").all()
            if broken:
                raise RuntimeError(f"Нарушены связи в файле группы: {broken[:5]}")
        engine.dispose()
        os.replace(tmp_path, target)
    except Exception:
        engine.dispose()
        tmp_path.unlink(missing_ok=True)
        raise
    return target
