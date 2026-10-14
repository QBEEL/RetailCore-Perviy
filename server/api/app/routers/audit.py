"""Журнал изменений: кто, когда и что менял.

Читать может только администратор. Для обычного пользователя это список чужих
действий, из которого видно, кто чем занимался, — сведения не его уровня.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import auditlog, db, security
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
    "attach": "вложение добавлено",
    "detach": "вложение убрано",
    "upload": "выгрузка локальной базы",
    "wipe": "удаление данных",
    "wipe_denied": "удаление отклонено: неверный пароль",
}

ENTITIES = {
    "payment": "оплата",
    "budget": "бюджет",
    "recipient_link": "привязка получателя",
    "app_user": "учётная запись",
    "database": "база данных",
    "dds_item": "статья ДДС",
    "instead_of": "оплата вместо другого",
}


class AuditEntry(BaseModel):
    id: int
    at: datetime
    user: str
    entity: str
    entity_id: int
    action: str
    # Код сущности («payment») — по нему окно записи запрашивает её историю;
    # `entity` выше — название для человека.
    kind: str = ""
    # Оплата заведена на получателя, у которого нет карточки поставщика.
    # Считается здесь: клиент не знает, привязана ли оплата к карточке.
    no_supplier: bool = False
    # О какой записи речь словами: получатель, сумма, дата. Номер без этого
    # ничего не говорит, а удалённую запись по номеру не найти нигде.
    subject: str = ""
    changes: dict[str, Any] = {}


def _subject(row: dict, changes: dict[str, Any]) -> str:
    """Что за запись. Снимок на момент действия — первым: сама запись могла
    с тех пор измениться или исчезнуть."""
    entity = row["entity"]
    if entity == "payment":
        about = changes.get(auditlog.ABOUT) or {}
        if about:
            return auditlog.describe(about)
        # Строки, записанные до появления снимка: берём оплату как она есть
        # сейчас, а если её удалили — получателя из записи о создании.
        if row["p_recipient"] is not None:
            return auditlog.describe({
                "recipient": row["p_recipient"], "amount": row["p_amount"],
                "pay_date": row["p_date"].isoformat() if row["p_date"] else "",
                "doc_number": row["p_doc"]})
        return row["first_recipient"] or ""
    if entity == "app_user":
        return row["t_user"] or ""
    if entity == "dds_item":
        return row["t_dds"] or changes.get("title") or ""
    if entity == "budget" and row["entity_id"]:
        year, month = divmod(row["entity_id"], 100)
        return f"{month:02d}.{year}"
    return ""


@router.get("", response_model=list[AuditEntry], summary="Журнал изменений")
def list_entries(
    user: User = Depends(security.admin_only),
    limit: int = 200,
    entity: str = "",
    login: str = "",
    record_id: int = 0,
    action: str = "",
    start: date | None = None,
    end: date | None = None,
    text: str = "",
) -> list[AuditEntry]:
    where, values = ["TRUE"], []
    if entity:
        where.append("a.entity = %s")
        values.append(entity)
    if login:
        where.append("u.login = %s")
        values.append(login)
    if record_id:
        where.append("a.entity_id = %s")
        values.append(record_id)
    if action:
        where.append("a.action = %s")
        values.append(action)
    if start:
        where.append("a.at >= %s")
        values.append(start)
    if end:
        # Конец включительно: «по 14.10» должно захватить весь день.
        where.append("a.at < %s")
        values.append(end + timedelta(days=1))
    if text.strip():
        # Ищем и в самих изменениях: снимок оплаты лежит в них, и так находится
        # получатель даже у оплаты, которой давно нет.
        like = f"%{text.strip()}%"
        where.append("(a.changes::text ILIKE %s OR p.recipient ILIKE %s"
                     " OR u.full_name ILIKE %s OR tu.full_name ILIKE %s)")
        values.extend([like] * 4)
    values.append(min(limit, 2000))

    rows = db.fetch_all(
        "SELECT a.id, a.at, COALESCE(u.full_name, u.login, '—') AS who,"
        "       a.entity, a.entity_id, a.action, a.changes,"
        "       p.recipient AS p_recipient, p.amount AS p_amount,"
        "       p.pay_date AS p_date, p.doc_number AS p_doc,"
        "       p.supplier_id AS p_supplier,"
        "       tu.full_name AS t_user, d.title AS t_dds,"
        "       (SELECT c.changes->>'recipient' FROM audit_log c"
        "         WHERE a.entity = 'payment' AND c.entity = 'payment'"
        "           AND c.entity_id = a.entity_id AND c.changes ? 'recipient'"
        "         ORDER BY c.id LIMIT 1) AS first_recipient"
        " FROM audit_log a LEFT JOIN app_user u ON u.id = a.user_id"
        " LEFT JOIN payment p ON a.entity = 'payment' AND a.entity_id > 0"
        "                    AND p.id = a.entity_id"
        " LEFT JOIN app_user tu ON a.entity = 'app_user' AND tu.id = a.entity_id"
        " LEFT JOIN dds_item d ON a.entity = 'dds_item' AND a.entity_id > 0"
        "                     AND d.id = a.entity_id"
        f" WHERE {' AND '.join(where)}"
        " ORDER BY a.id DESC LIMIT %s", values)

    entries = []
    for row in rows:
        changes = dict(row["changes"] or {})
        subject = _subject(row, changes)
        # Снимок уже показан в «Записи» — в изменениях его повторять незачем.
        changes.pop(auditlog.ABOUT, None)
        entries.append(AuditEntry(
            id=row["id"], at=row["at"], user=row["who"],
            entity=ENTITIES.get(row["entity"], row["entity"]),
            entity_id=row["entity_id"],
            action=ACTIONS.get(row["action"], row["action"]),
            kind=row["entity"],
            no_supplier=(row["entity"] == "payment" and row["action"] == "create"
                         and row["p_recipient"] is not None
                         and not row["p_supplier"]),
            subject=subject, changes=changes))
    return entries
