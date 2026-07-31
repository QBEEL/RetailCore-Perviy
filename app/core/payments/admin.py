"""Управление учётными записями и чтение журнала изменений.

Всё здесь доступно только администратору — сервер откажет остальным, но
интерфейс и не должен показывать раздел тому, кто не сможет им пользоваться.

Модуль намеренно отделён от `remote`: там оплаты, которыми пользуются все,
здесь — обслуживание, которым занимается один человек.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import transport


@dataclass(slots=True)
class Account:
    """Учётная запись менеджера."""

    id: int = 0
    login: str = ""
    full_name: str = ""
    # Значения `responsible` из выгрузки 1С, которые считаются «своими».
    responsible: list[str] = field(default_factory=list)
    is_admin: bool = False
    is_active: bool = True
    created_at: datetime | None = None

    @property
    def title(self) -> str:
        return self.full_name or self.login

    @property
    def role(self) -> str:
        if not self.is_active:
            return "отключена"
        return "администратор" if self.is_admin else "менеджер"


@dataclass(slots=True)
class Entry:
    """Строка журнала изменений."""

    id: int = 0
    at: datetime | None = None
    user: str = ""
    entity: str = ""
    entity_id: int = 0
    action: str = ""
    changes: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """Что именно изменилось — одной строкой для таблицы."""
        if not self.changes:
            return ""
        parts = []
        for name, value in self.changes.items():
            parts.append(f"{FIELD_TITLES.get(name, name)}: {value}")
        return " · ".join(parts)


# Названия полей для журнала. Без них в таблице стояло бы «pay_date».
FIELD_TITLES = {
    "pay_date": "дата оплаты",
    "status": "статус",
    "comment": "комментарий",
    "supplier_id": "поставщик",
    "amount": "сумма",
    "priority": "приоритет",
    "paid_flag": "отметка об оплате",
    "recipient": "получатель",
    "ids": "записей",
    "note": "примечание",
}


def _moment(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


def _account(row: dict) -> Account:
    return Account(
        id=int(row["id"]), login=row["login"], full_name=row["full_name"],
        responsible=list(row.get("responsible", [])),
        is_admin=bool(row["is_admin"]), is_active=bool(row["is_active"]),
        created_at=_moment(row.get("created_at")))


# --- учётные записи ------------------------------------------------------------

def accounts() -> list[Account]:
    return [_account(row) for row in transport.get("/api/users")]


def create(account: Account) -> tuple[Account, str]:
    """Заводит учётку. Возвращает её и пароль — он показывается один раз."""
    answer = transport.post("/api/users", {
        "login": account.login, "full_name": account.full_name,
        "responsible": account.responsible, "is_admin": account.is_admin,
        "is_active": account.is_active})
    account.id = int(answer["id"])
    account.login = answer["login"]
    return account, answer["password"]


def save(account: Account) -> Account:
    return _account(transport.put(f"/api/users/{account.id}", {
        "login": account.login, "full_name": account.full_name,
        "responsible": account.responsible, "is_admin": account.is_admin,
        "is_active": account.is_active}))


def reset_password(account_id: int) -> str:
    """Назначает новый пароль. Прежний восстановить нельзя — только заменить."""
    return transport.post(f"/api/users/{account_id}/password")["password"]


# --- журнал --------------------------------------------------------------------

def journal(limit: int = 200, entity: str = "", login: str = "") -> list[Entry]:
    rows = transport.get("/api/audit",
                         {"limit": limit, "entity": entity, "login": login})
    return [
        Entry(id=row["id"], at=_moment(row["at"]), user=row["user"],
              entity=row["entity"], entity_id=row["entity_id"],
              action=row["action"], changes=row.get("changes") or {})
        for row in rows
    ]
