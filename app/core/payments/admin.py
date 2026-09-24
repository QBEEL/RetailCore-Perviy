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
    # Коды направлений: beauty, fashion. Заполнены у категорийных менеджеров и
    # пусты у всех остальных — бухгалтерия и маркетинг оплаты проводят, но
    # поставщиков не ведут, и закрепление им предлагать не за чем.
    directions: list[str] = field(default_factory=list)
    # Разделы приложения, закрытые этой учётке (коды из app/ui/pages.py).
    # Закрытые, а не открытые: раздел, добавленный новой версией, должен
    # появиться у всех сам, без обхода учёток с раздачей прав.
    denied_pages: list[str] = field(default_factory=list)
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
        if self.is_admin:
            return "администратор"
        return "категорийный менеджер" if self.directions else "менеджер"


@dataclass(slots=True)
class Scope:
    """Что стоит в общей базе — и что из этого стирается удалением.

    Считается сервером перед показом диалога: соглашаться на удаление, не видя
    его объёма, человеку не на чем.
    """

    payments: int = 0
    files: int = 0
    budgets: int = 0
    imports: int = 0
    links: int = 0
    # Умеет ли сервер удалять данные. Пустой объём и необновлённый сервер — это
    # разные ответы, и путать их нельзя: «данных нет» при полной базе сбивает с
    # толку сильнее, чем прямое «сервер не обновлён».
    supported: bool = True

    @property
    def empty(self) -> bool:
        return not self.total

    @property
    def total(self) -> int:
        return (self.payments + self.files + self.budgets
                + self.imports + self.links)

    @property
    def summary(self) -> str:
        """Перечисление непустого — для диалога и для сообщения после."""
        parts = [f"{value} {title}" for title, value in (
            (_plural(self.payments, "оплата", "оплаты", "оплат"), self.payments),
            (_plural(self.files, "вложение", "вложения", "вложений"), self.files),
            (_plural(self.budgets, "бюджет", "бюджета", "бюджетов"), self.budgets),
            (_plural(self.imports, "импорт", "импорта", "импортов"), self.imports),
            (_plural(self.links, "привязка", "привязки", "привязок"), self.links),
        ) if value]
        return ", ".join(parts) if parts else "ничего"


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Форма слова при числе: 1 оплата, 2 оплаты, 5 оплат."""
    tail, tens = count % 10, count % 100
    if tens in range(11, 20) or tail == 0 or tail >= 5:
        return many
    return one if tail == 1 else few


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
    # Что стёрло удаление данных: журнал остаётся, и объём удаления виден в нём.
    "payments": "оплат",
    "files": "вложений",
    "budgets": "бюджетов",
    "imports": "импортов",
    "links": "привязок",
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
        directions=list(row.get("directions", []) or []),
        denied_pages=list(row.get("denied_pages", []) or []),
        is_admin=bool(row["is_admin"]), is_active=bool(row["is_active"]),
        created_at=_moment(row.get("created_at")))


# --- учётные записи ------------------------------------------------------------

def accounts() -> list[Account]:
    return [_account(row) for row in transport.get("/api/users")]


def create(account: Account) -> tuple[Account, str]:
    """Заводит учётку. Возвращает её и пароль — он показывается один раз."""
    answer = transport.post("/api/users", {
        "login": account.login, "full_name": account.full_name,
        "responsible": account.responsible, "directions": account.directions,
        "denied_pages": account.denied_pages,
        "is_admin": account.is_admin, "is_active": account.is_active})
    account.id = int(answer["id"])
    account.login = answer["login"]
    return account, answer["password"]


def save(account: Account) -> Account:
    return _account(transport.put(f"/api/users/{account.id}", {
        "login": account.login, "full_name": account.full_name,
        "responsible": account.responsible, "directions": account.directions,
        "denied_pages": account.denied_pages,
        "is_admin": account.is_admin, "is_active": account.is_active}))


def reset_password(account_id: int) -> str:
    """Назначает новый пароль. Прежний восстановить нельзя — только заменить."""
    return transport.post(f"/api/users/{account_id}/password")["password"]


# --- удаление данных -----------------------------------------------------------

# Слово подтверждения. Такое же ждёт сервер: набрать его — единственный способ
# сказать «да» этому действию.
CONFIRM = "УДАЛИТЬ"


def _scope(row: dict) -> Scope:
    return Scope(payments=int(row.get("payments", 0)),
                 files=int(row.get("files", 0)),
                 budgets=int(row.get("budgets", 0)),
                 imports=int(row.get("imports", 0)),
                 links=int(row.get("links", 0)))


def scope() -> Scope:
    """Объём базы: сколько чего сотрёт удаление.

    Сервер прежней версии об удалении не знает и отвечает «нет такого адреса».
    Отдельным признаком, а не пустым объёмом: раздел из-за этого открываться не
    перестаёт, но и сказать «данных нет» при полной базе он не должен.
    """
    try:
        return _scope(transport.get("/api/maintenance/scope") or {})
    except transport.ServerError as error:
        if error.status == 404:
            return Scope(supported=False)
        raise


def wipe(password: str) -> Scope:
    """Стирает оплаты и всё при них. Возвращает то, что было удалено.

    Пароль уходит на сервер вместе с запросом: токена для такого действия мало —
    он лежит в памяти открытого приложения, а пароль знает только владелец
    учётной записи.
    """
    return _scope(transport.post("/api/maintenance/wipe",
                                 {"confirm": CONFIRM, "password": password}) or {})


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
