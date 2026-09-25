"""Направления и закрепление поставщиков за менеджерами.

Живёт на сервере, а не в `suppliers.db`. Карточка поставщика у каждого своя —
она про разбор прайса на конкретной машине, — а закрепление и направление одни
на отдел: проставленное одним человеком должно быть видно остальным, иначе
фильтр «мои поставщики» у каждого показывал бы свою правду.

Отдельный модуль, а не `store`/`remote`, потому что локального источника у этих
данных нет вовсе. Без входа в общую базу список закреплений пуст, вкладка
работает как раньше, а кнопки закрепления гаснут — это честнее, чем показывать
закрепление, которое некуда сохранить.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote

from ..payments import transport

# Черновик — менеджер заявил и может отозвать сам.
# Зафиксировано — администратор подтвердил, правит дальше только он.
DRAFT = "draft"
FIXED = "fixed"


@dataclass(slots=True)
class Direction:
    """Направление отдела: Beauty, Fashion."""

    code: str = ""
    title: str = ""
    id: int = 0
    sort_order: int = 0


@dataclass(slots=True)
class AssignedManager:
    """Менеджер, закреплённый за поставщиком."""

    user_id: int = 0
    full_name: str = ""
    state: str = DRAFT
    directions: list[str] = field(default_factory=list)

    @property
    def fixed(self) -> bool:
        return self.state == FIXED

    @property
    def title(self) -> str:
        return self.full_name if self.fixed else f"{self.full_name} (на подтверждении)"


@dataclass(slots=True)
class Entry:
    """Строка списка поставщиков.

    `managers` и `assigned` — разные ответы на разные вопросы, и сводить их в
    одно поле нельзя: первое про то, кто платил, второе про то, кто ведёт.
    Именно их смешение и делало прежний список бесполезным.
    """

    recipient_key: str = ""
    recipient: str = ""
    supplier_id: int = 0
    payments: int = 0
    amount: float = 0.0
    last_pay: date | None = None
    managers: list[str] = field(default_factory=list)
    assigned: list[AssignedManager] = field(default_factory=list)
    directions: list[str] = field(default_factory=list)
    unassigned_payers: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.recipient

    @property
    def assigned_title(self) -> str:
        """Кто ведёт. Пусто — поставщик ещё не разобран."""
        if not self.assigned:
            return ""
        return ", ".join(person.title for person in self.assigned)

    @property
    def has_draft(self) -> bool:
        return any(not person.fixed for person in self.assigned)

    def mine(self, user_id: int) -> bool:
        """Мой ли это поставщик. Черновик считается своим наравне с фиксацией."""
        return any(person.user_id == user_id for person in self.assigned)


@dataclass(slots=True)
class Page:
    """Страница списка: сервер отбирает и сортирует, клиент только показывает."""

    items: list[Entry] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 100

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size)) if self.page_size else 1


@dataclass(slots=True)
class Result:
    """Итог массового действия."""

    changed: int = 0
    skipped: list[str] = field(default_factory=list)


def online() -> bool:
    """Доступны ли закрепления. Без входа в общую базу их просто нет."""
    return transport.session.active


def directions() -> list[Direction]:
    if not online():
        return []
    return [Direction(id=row["id"], code=row["code"], title=row["title"],
                      sort_order=row["sort_order"])
            for row in transport.get("/api/suppliers/directions")]


def direction_keys() -> dict[str, tuple[str, ...]]:
    """Ключ получателя → его направления, только у кого они есть.

    Нужно отбору оплат по направлению. Список собирается из той же страницы
    поставщиков, что показывает вкладка «Поставщики», — правило, по которому
    поставщик попадает в направление, остаётся одно, на сервере. Без входа
    направлений нет, и словарь пуст.
    """
    if not online():
        return {}
    found: dict[str, tuple[str, ...]] = {}
    size = 500
    for item in directions():
        page = 1
        while True:
            answer = suppliers(direction=item.code, sort="name", order="asc",
                               page=page, page_size=size)
            for entry in answer.items:
                found[entry.recipient_key] = tuple(entry.directions)
            if not answer.items or page * size >= answer.total:
                break
            page += 1
    return found


def managers() -> list[tuple[int, str]]:
    """Менеджеры для фильтра: только те, за кем что-то закреплено."""
    if not online():
        return []
    return [(int(row["user_id"]), row["full_name"])
            for row in transport.get("/api/suppliers/managers")]


def suppliers(search: str = "", direction: str = "", manager: int = 0,
              state: str = "", sort: str = "amount", order: str = "desc",
              page: int = 1, page_size: int = 100) -> Page:
    """Страница списка поставщиков с закреплением и направлениями."""
    answer = transport.get("/api/suppliers", {
        "search": search or None,
        "direction": direction or None,
        "manager": manager or None,
        "state": state or None,
        "sort": sort,
        "order": order,
        "page": page,
        "page_size": page_size,
    })
    return _page(answer)


def pending_claims(page: int = 1, page_size: int = 200) -> Page:
    """Очередь заявок на подтверждение. Доступна только администратору."""
    return _page(transport.get("/api/suppliers/claims",
                               {"page": page, "page_size": page_size}))


@dataclass(slots=True)
class Conflict:
    """Поставщик, которому я платил, не будучи за ним закреплён."""

    recipient_key: str = ""
    recipient: str = ""
    payments: int = 0
    amount: float = 0.0
    assigned: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        who = ", ".join(self.assigned) if self.assigned else "никто"
        return f"{self.recipient} — ведёт {who}"


def conflicts() -> list[Conflict]:
    """Мои оплаты в пользу чужих поставщиков. Предупреждение, а не запрет."""
    if not online():
        return []
    return [Conflict(recipient_key=row["recipient_key"],
                     recipient=row["recipient"], payments=int(row["payments"]),
                     amount=float(row["amount"] or 0.0),
                     assigned=list(row["assigned"] or []))
            for row in transport.get("/api/suppliers/conflicts")]


def claim(keys: list[str]) -> Result:
    """Заявить поставщиков своими."""
    return _result(transport.post("/api/suppliers/claims", {"keys": keys}))


def withdraw(keys: list[str]) -> Result:
    """Отозвать свою заявку. Зафиксированное так снять нельзя."""
    return _result(transport.post("/api/suppliers/claims/withdraw", {"keys": keys}))


def fix(keys: list[str], user_ids: list[int] | None = None) -> Result:
    """Зафиксировать закрепление. Только администратор."""
    return _result(transport.post("/api/suppliers/assignments/fix",
                                  {"keys": keys, "user_ids": user_ids or []}))


def unassign(keys: list[str], user_ids: list[int] | None = None) -> Result:
    """Снять закрепление, в том числе зафиксированное. Только администратор."""
    return _result(transport.post("/api/suppliers/assignments/unassign",
                                  {"keys": keys, "user_ids": user_ids or []}))


def set_directions(recipient_key: str, codes: list[str]) -> list[str]:
    """Задать направления вручную. Пустой список вернёт автоматический расчёт.

    Ключ кодируется целиком: в нём кириллица и пробелы, а в именах бывают
    «/» и «?» — без кодирования запрос ушёл бы не по тому адресу или не ушёл
    бы вовсе.
    """
    return list(transport.patch(
        f"/api/suppliers/{quote(recipient_key, safe='')}/directions",
        {"codes": codes}))


def _page(answer: dict) -> Page:
    return Page(
        items=[_entry(row) for row in answer.get("items", [])],
        total=int(answer.get("total", 0)),
        page=int(answer.get("page", 1)),
        page_size=int(answer.get("page_size", 100)),
    )


def _entry(row: dict) -> Entry:
    return Entry(
        recipient_key=row["recipient_key"],
        recipient=row["recipient"],
        supplier_id=int(row.get("supplier_id") or 0),
        payments=int(row.get("payments") or 0),
        amount=float(row.get("amount") or 0.0),
        last_pay=_date(row.get("last_pay")),
        managers=list(row.get("managers") or []),
        assigned=[AssignedManager(
            user_id=int(person["user_id"]), full_name=person["full_name"],
            state=person["state"], directions=list(person.get("directions") or []))
            for person in row.get("assigned") or []],
        directions=list(row.get("directions") or []),
        unassigned_payers=list(row.get("unassigned_payers") or []),
    )


def _result(answer: dict) -> Result:
    return Result(changed=int(answer.get("changed", 0)),
                  skipped=list(answer.get("skipped") or []))


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
