from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AuditLog


def log(
    db: Session,
    *,
    action: str,
    group_id: int | None = None,
    actor_id: int | None = None,
    entity_type: str = "",
    entity_id: int | None = None,
    payload: dict | None = None,
) -> AuditLog:
    entry = AuditLog(
        action=action,
        group_id=group_id,
        actor_id=actor_id,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload,
    )
    db.add(entry)
    return entry
