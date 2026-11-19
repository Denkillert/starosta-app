from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import DocStatus, DocType, Role
from ..models import Document, Group, Membership, User
from ..utils import now_local
from . import audit
from .attendance import apply_document, revert_document

DEFAULT_STATUS: dict[str, str] = {
    DocType.ORDER: DocStatus.ACTIVE,
    DocType.CERTIFICATE: DocStatus.ACTIVE,
    DocType.STATEMENT: DocStatus.PENDING,
}

PREFIXES: dict[str, str] = {
    DocType.ORDER: "ОРД",
    DocType.CERTIFICATE: "СПР",
    DocType.STATEMENT: "ЗАЯ",
}

DEFAULT_TITLES: dict[str, str] = {
    DocType.ORDER: "Освобождение от занятий",
    DocType.CERTIFICATE: "Справка",
    DocType.STATEMENT: "Заявление",
}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    DocStatus.DRAFT: {DocStatus.ACTIVE, DocStatus.PENDING, DocStatus.ARCHIVED},
    DocStatus.PENDING: {DocStatus.APPROVED, DocStatus.REJECTED, DocStatus.ARCHIVED},
    DocStatus.APPROVED: {DocStatus.ACTIVE, DocStatus.ARCHIVED},
    DocStatus.ACTIVE: {DocStatus.REVOKED, DocStatus.ARCHIVED},
    DocStatus.REVOKED: {DocStatus.ACTIVE, DocStatus.ARCHIVED},
    DocStatus.REJECTED: {DocStatus.PENDING, DocStatus.ARCHIVED},
    DocStatus.ARCHIVED: {DocStatus.DRAFT},
}

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".doc", ".docx", ".txt"}


class DocumentError(ValueError):
    pass


def next_number(db: Session, group_id: int, type_: str) -> str:
    """Следующий номер в рамках года и типа документа.

    Раньше номер считался по количеству документов: после удаления одного из
    них новый документ получал уже занятый номер.
    """
    year = now_local().year
    prefix = PREFIXES.get(type_, "ДОК")
    existing = db.scalars(
        select(Document.number).where(
            Document.group_id == group_id,
            Document.type == type_,
            Document.number.like(f"{prefix}-{year}-%"),
        )
    ).all()
    last = 0
    for value in existing:
        tail = str(value or "").rsplit("-", 1)[-1]
        if tail.isdigit():
            last = max(last, int(tail))
    return f"{prefix}-{year}-{last + 1:03d}"


def _ensure_unique_number(
    db: Session,
    *,
    group_id: int,
    type_: str,
    number: str,
    exclude_id: int | None = None,
) -> None:
    stmt = select(Document.id).where(
        Document.group_id == group_id,
        Document.type == type_,
        Document.number == number,
    )
    if exclude_id is not None:
        stmt = stmt.where(Document.id != exclude_id)
    if db.scalar(stmt):
        raise DocumentError(f"Документ с номером «{number}» уже существует")


def create_document(
    db: Session,
    *,
    group: Group,
    author: User | None,
    type_: str,
    title: str | None = None,
    number: str | None = None,
    student_id: int | None = None,
    student_ids: list[int] | None = None,
    issued_at: date | None = None,
    valid_from: date | None = None,
    valid_to: date | None = None,
    valid_from_time=None,
    valid_to_time=None,
    body: str = "",
    reason: str | None = None,
    lesson_ids: list[int] | None = None,
    status: str | None = None,
    meta: dict | None = None,
    commit: bool = True,
) -> Document:
    if type_ not in DocType.ALL:
        raise DocumentError("Неизвестный тип документа")

    ids = list(dict.fromkeys(student_ids or ([student_id] if student_id else [])))
    students: list[Membership] = []
    if ids:
        students = list(
            db.scalars(
                select(Membership).where(
                    Membership.id.in_(ids),
                    Membership.group_id == group.id,
                )
            ).all()
        )
        if len(students) != len(ids):
            raise DocumentError("Студент не найден в этой группе")

    if not students:
        raise DocumentError("Выберите хотя бы одного студента")
    if type_ in (DocType.ORDER, DocType.CERTIFICATE) and valid_from is None:
        raise DocumentError("Укажите дату начала периода освобождения")
    if valid_from and valid_to and valid_from > valid_to:
        raise DocumentError("Дата начала позже даты окончания")
    if (
        valid_from
        and valid_to
        and valid_from == valid_to
        and valid_from_time
        and valid_to_time
        and valid_from_time > valid_to_time
    ):
        raise DocumentError("Время начала позже времени окончания")

    number_value = (number or "").strip()
    if type_ == DocType.ORDER and not number_value:
        raise DocumentError("Укажите номер распоряжения")

    final_status = status or DEFAULT_STATUS.get(type_, DocStatus.DRAFT)
    if final_status not in DocStatus.ALL:
        raise DocumentError("Неизвестный статус документа")

    # Пользовательское название сохраняется для любого типа документа,
    # а не только для заявления (раньше «Справка по болезни» превращалась
    # в безликое «Справка»).
    final_title = (title or "").strip() or DEFAULT_TITLES.get(type_, "Документ")

    number_value = number_value or next_number(db, group.id, type_)
    _ensure_unique_number(
        db, group_id=group.id, type_=type_, number=number_value
    )

    document = Document(
        group_id=group.id,
        type=type_,
        status=final_status,
        title=final_title,
        number=number_value,
        student_id=students[0].id if students else None,
        author_id=author.id if author else None,
        issued_at=issued_at or now_local().date(),
        valid_from=valid_from,
        valid_to=valid_to,
        valid_from_time=valid_from_time,
        valid_to_time=valid_to_time,
        body=body.strip(),
        reason=reason,
        lesson_ids=lesson_ids or None,
        meta=meta or {},
    )
    db.add(document)
    db.flush()
    if students:
        document.students = students

    if document.type in (DocType.ORDER, DocType.CERTIFICATE):
        apply_document(db, document)

    audit.log(
        db,
        action="document.create",
        group_id=group.id,
        actor_id=author.id if author else None,
        entity_type="document",
        entity_id=document.id,
        payload={"type": type_, "status": final_status, "number": document.number},
    )
    if commit:
        db.commit()
    return document


def update_document(
    db: Session,
    *,
    document: Document,
    actor: User | None = None,
    **fields,
) -> Document:
    editable = {
        "title",
        "number",
        "student_id",
        "student_ids",
        "issued_at",
        "valid_from",
        "valid_to",
        "valid_from_time",
        "valid_to_time",
        "body",
        "reason",
        "lesson_ids",
        "status",
        "meta",
    }
    unknown = set(fields) - editable
    if unknown:
        raise DocumentError(f"Нельзя изменить поля: {', '.join(sorted(unknown))}")

    if "title" in fields:
        fields["title"] = (
            str(fields["title"] or "").strip()
            or DEFAULT_TITLES.get(document.type, "Документ")
        )
    if fields.get("number") is not None:
        fields["number"] = str(fields["number"]).strip() or None

    touches_excuses = bool(
        {
            "student_id",
            "student_ids",
            "valid_from",
            "valid_to",
            "valid_from_time",
            "valid_to_time",
            "lesson_ids",
            "status",
        }
        & set(fields)
    )

    # Сначала проверяем новые значения и только потом снимаем старые «У»:
    # иначе отклонённое редактирование оставляло журнал изменённым.
    students: list[Membership] | None = None
    if "student_ids" in fields:
        ids = list(dict.fromkeys(fields.pop("student_ids") or []))
        students = list(
            db.scalars(
                select(Membership).where(
                    Membership.id.in_(ids),
                    Membership.group_id == document.group_id,
                )
            ).all()
        ) if ids else []
        if ids and len(students) != len(ids):
            raise DocumentError("Студент не найден в этой группе")
        if not students:
            raise DocumentError("Выберите хотя бы одного студента")

    new_student_id = fields.get("student_id")
    if new_student_id:
        student = db.get(Membership, new_student_id)
        if student is None or student.group_id != document.group_id:
            raise DocumentError("Студент не найден в этой группе")

    new_valid_from = fields.get("valid_from", document.valid_from)
    new_valid_to = fields.get("valid_to", document.valid_to)
    if new_valid_from and new_valid_to and new_valid_from > new_valid_to:
        raise DocumentError("Дата начала позже даты окончания")

    new_from_time = fields.get("valid_from_time", document.valid_from_time)
    new_to_time = fields.get("valid_to_time", document.valid_to_time)
    if (
        new_valid_from
        and new_valid_to
        and new_valid_from == new_valid_to
        and new_from_time
        and new_to_time
        and new_from_time > new_to_time
    ):
        raise DocumentError("Время начала позже времени окончания")

    new_status = fields.get("status", document.status)
    if new_status not in DocStatus.ALL:
        raise DocumentError("Неизвестный статус документа")

    new_number = fields.get("number")
    if new_number:
        _ensure_unique_number(
            db,
            group_id=document.group_id,
            type_=document.type,
            number=str(new_number).strip(),
            exclude_id=document.id,
        )

    if touches_excuses:
        revert_document(db, document)

    if students is not None:
        document.students = students
        document.student_id = students[0].id if students else None

    for key, value in fields.items():
        setattr(document, key, value)

    if touches_excuses:
        apply_document(db, document)

    audit.log(
        db,
        action="document.update",
        group_id=document.group_id,
        actor_id=actor.id if actor else None,
        entity_type="document",
        entity_id=document.id,
        payload={"fields": sorted(fields)},
    )
    db.commit()
    return document


def set_status(
    db: Session,
    *,
    document: Document,
    status: str,
    actor: User | None = None,
    comment: str | None = None,
) -> Document:
    if status not in DocStatus.ALL:
        raise DocumentError("Неизвестный статус")
    if status != document.status:
        allowed = ALLOWED_TRANSITIONS.get(document.status, set())
        if status not in allowed:
            raise DocumentError(
                f"Нельзя перевести документ из «{DocStatus.LABELS.get(document.status)}» "
                f"в «{DocStatus.LABELS.get(status)}»"
            )

    previous = document.status
    document.status = status
    if comment:
        document.meta = {**(document.meta or {}), "last_comment": comment}

    if status != previous:
        if status in (DocStatus.REVOKED, DocStatus.ARCHIVED, DocStatus.REJECTED):
            revert_document(db, document)
        elif status in (DocStatus.ACTIVE, DocStatus.APPROVED):
            apply_document(db, document)

    audit.log(
        db,
        action="document.status",
        group_id=document.group_id,
        actor_id=actor.id if actor else None,
        entity_type="document",
        entity_id=document.id,
        payload={"from": previous, "to": status},
    )
    db.commit()
    return document


def attach_file(
    db: Session,
    *,
    document: Document,
    stored_name: str,
    original_name: str,
    actor: User | None = None,
) -> Document:
    _remove_file(document.file_path)
    document.file_path = stored_name
    document.file_name = original_name
    audit.log(
        db,
        action="document.file",
        group_id=document.group_id,
        actor_id=actor.id if actor else None,
        entity_type="document",
        entity_id=document.id,
        payload={"file": original_name},
    )
    db.commit()
    return document


def delete_document(db: Session, *, document: Document, actor: User | None = None) -> None:
    if document.type in (DocType.ORDER, DocType.CERTIFICATE):
        revert_document(db, document)
    _remove_file(document.file_path)
    group_id = document.group_id
    document_id = document.id
    db.delete(document)
    audit.log(
        db,
        action="document.delete",
        group_id=group_id,
        actor_id=actor.id if actor else None,
        entity_type="document",
        entity_id=document_id,
    )
    db.commit()


def save_bytes(data: bytes, filename: str) -> str:
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise DocumentError(f"Файл больше {settings.max_upload_mb} МБ")
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise DocumentError(
            "Можно прикладывать PDF, изображения или документы Word"
        )
    stored = f"{uuid.uuid4().hex}{suffix}"
    (settings.upload_dir / stored).write_bytes(data)
    return stored


def file_path(document: Document) -> Path | None:
    if not document.file_path:
        return None
    path = settings.upload_dir / document.file_path
    return path if path.exists() else None


def _remove_file(stored_name: str | None) -> None:
    if not stored_name:
        return
    path = settings.upload_dir / stored_name
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def list_documents(
    db: Session,
    group_id: int,
    *,
    type_: str | None = None,
    status: str | None = None,
    student_id: int | None = None,
    limit: int = 200,
) -> list[Document]:
    stmt = select(Document).where(Document.group_id == group_id)
    if type_:
        stmt = stmt.where(Document.type == type_)
    if status:
        stmt = stmt.where(Document.status == status)
    if student_id:
        stmt = stmt.where(Document.student_id == student_id)
    stmt = stmt.order_by(Document.issued_at.desc(), Document.id.desc()).limit(limit)
    return list(db.scalars(stmt).all())


def counts_by_type(db: Session, group_id: int) -> dict[str, int]:
    rows = db.execute(
        select(Document.type, func.count(Document.id))
        .where(Document.group_id == group_id)
        .group_by(Document.type)
    ).all()
    result = {type_: 0 for type_ in DocType.ALL}
    for type_, count in rows:
        result[type_] = count
    return result


def can_view(document: Document, membership: Membership) -> bool:
    if membership.role in Role.STAFF:
        return True
    if document.type == DocType.ORDER:
        return True
    student_ids = {student.id for student in document.students}
    if document.student_id:
        student_ids.add(document.student_id)
    return membership.id in student_ids
