"""Журнал изменений: кто, когда и что менял.

Читать может только администратор. Для обычного пользователя это список чужих
действий, из которого видно, кто чем занимался, — сведения не его уровня.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import db, security
from ..security import User

router = APIRouter(prefix="/api/audit", tags=["Журнал"])

# Человеческие названия действий. Английские слова в журнале читал бы только
# тот, кто его писал.
ACTIONS = {
    "create": "создание",
    "update": "изменение",
    "update_many": "массовое изменение",
    "delete": "удаление",
    "save": "сохранение",
    "password": "смена пароля",
    "reset_password": "сброс пароля",
    "import": "импорт из 1С",
}

ENTITIES = {
    "payment": "оплата",
    "budget": "бюджет",
    "recipient_link": "привязка получателя",
    "app_user": "учётная запись",
}


class AuditEntry(BaseModel):
    id: int
    at: datetime
    user: str
    entity: str
    entity_id: int
    action: str
    changes: dict[str, Any] = {}


@router.get("", response_model=list[AuditEntry], summary="Журнал изменений")
def list_entries(
    user: User = Depends(security.admin_only),
    limit: int = 200,
    entity: str = "",
    login: str = "",
) -> list[AuditEntry]:
    where, values = ["TRUE"], []
    if entity:
        where.append("a.entity = %s")
        values.append(entity)
    if login:
        where.append("u.login = %s")
        values.append(login)
    values.append(min(limit, 2000))

    rows = db.fetch_all(
        "SELECT a.id, a.at, COALESCE(u.full_name, u.login, '—') AS who,"
        "       a.entity, a.entity_id, a.action, a.changes"
        " FROM audit_log a LEFT JOIN app_user u ON u.id = a.user_id"
        f" WHERE {' AND '.join(where)}"
        " ORDER BY a.id DESC LIMIT %s", values)

    return [
        AuditEntry(
            id=row["id"], at=row["at"], user=row["who"],
            entity=ENTITIES.get(row["entity"], row["entity"]),
            entity_id=row["entity_id"],
            action=ACTIONS.get(row["action"], row["action"]),
            changes=row["changes"] or {},
        )
        for row in rows
    ]
