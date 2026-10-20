"""Управление учётными записями и чтение журнала изменений.

Всё здесь доступно только администратору — сервер откажет остальным, но
интерфейс и не должен показывать раздел тому, кто не сможет им пользоваться.

Модуль намеренно отделён от `remote`: там оплаты, которыми пользуются все,
здесь — обслуживание, которым занимается один человек.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from . import transport
from .models import STATUS_TITLES, PaymentStatus


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
    # Личный предел обращений к ИИ-правке в сутки: None — общий предел сервера,
    # 0 — ИИ человеку закрыт. ai_used — сколько обращений сегодня.
    ai_limit: int | None = None
    ai_used: int = 0
    # Версия приложения у человека и когда он последний раз обращался к серверу.
    # Пусто — версия старше той, что научилась себя называть.
    app_version: str = ""
    app_platform: str = ""
    app_seen_at: datetime | None = None

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
    # О какой записи речь словами — «Получатель · 100 000 ₽ · 14.10.2026».
    # Номер сам по себе ничего не говорит, а удалённую запись по нему не найти.
    subject: str = ""
    # Код сущности («payment»); пуст у сервера прежней версии.
    kind: str = ""
    # Оплата заведена на получателя без карточки поставщика (считает сервер).
    no_supplier: bool = False
    changes: dict[str, Any] = field(default_factory=dict)

    @property
    def is_payment(self) -> bool:
        return self.kind == "payment" or (not self.kind and self.entity == "оплата")

    @property
    def flags(self) -> list[str]:
        """Что в этом действии стоит проверить глазами.

        Это не обвинение, а повод посмотреть: удаление, заметная правка суммы,
        оплата получателю без карточки поставщика, оплата, заведённая в
        выходной. Порог и перечень один на всех — чтобы подсвеченное значило
        одно и то же в любой день.
        """
        if not self.is_payment:
            return []
        found = []
        if self.action == "удаление":
            found.append("оплата удалена")
        change = self.amount_change()
        if change is not None and abs(change) >= AMOUNT_ALERT:
            found.append(f"сумма изменена на {abs(change):.0%}".replace("%", " %"))
        if self.action == "создание":
            if self.no_supplier:
                found.append("нет карточки поставщика")
            if self.at is not None and self.at.weekday() >= 5:
                found.append("заведена в выходной")
        return found

    def amount_change(self) -> float | None:
        """Доля, на которую изменилась сумма; `None` — сумма не менялась."""
        was = (self.changes.get("was") or {}).get("amount", self.changes.get("amount_was"))
        now = self.changes.get("amount")
        try:
            was, now = float(was), float(now)
        except (TypeError, ValueError):
            return None
        return (now - was) / was if was else None

    @property
    def record(self) -> str:
        """Колонка «Запись»: номер и то, что за ним стоит."""
        number = f"№{self.entity_id}" if self.entity_id else ""
        return " · ".join(part for part in (number, self.subject) if part)

    def rows(self) -> list[tuple[str, str, str]]:
        """Изменения построчно: поле, было, стало — для окна записи.

        Здесь ничего не пропускается и не сокращается: окно существует ради
        полной картины, в отличие от одной строки таблицы. Список (оплаты
        массовой правки) разворачивается по строке на элемент.
        """
        changes = dict(self.changes)
        was = dict(changes.pop("was", None) or {})
        if "amount_was" in changes:
            was.setdefault("amount", changes.pop("amount_was"))
        rows: list[tuple[str, str, str]] = []
        for name, value in changes.items():
            title = FIELD_TITLES.get(name, name)
            if isinstance(value, (list, tuple)):
                rows.extend((title if index == 0 else "", "", str(item))
                            for index, item in enumerate(value))
                continue
            rows.append((title, _show(name, was[name]) if name in was else "",
                         _show(name, value)))
        return rows

    @property
    def summary(self) -> str:
        """Что именно изменилось — одной строкой для таблицы.

        Если сервер прислал прежние значения, показывается «было → стало».
        """
        changes = dict(self.changes)
        was = dict(changes.pop("was", None) or {})
        if "amount_was" in changes:
            # Так сервер записывал прежнюю сумму до появления `was`.
            was.setdefault("amount", changes.pop("amount_was"))
        parts = []
        for name, value in changes.items():
            if name == "recipient" and str(value) in self.subject:
                continue
            shown = _show(name, value)
            if name in was:
                shown = f"{_show(name, was[name])} → {shown}"
            parts.append(f"{FIELD_TITLES.get(name, name)}: {shown}")
        return " · ".join(parts)


# Варианты отбора журнала: подпись и код, который понимает сервер.
ENTITY_FILTER = (("Всё", ""), ("Оплаты", "payment"), ("Бюджеты", "budget"),
                 ("Учётные записи", "app_user"), ("Статьи ДДС", "dds_item"),
                 ("Привязки получателей", "recipient_link"))
ACTION_FILTER = (("Любое действие", ""), ("Создание", "create"), ("Изменение", "update"),
                 ("Массовое изменение", "update_many"), ("Удаление", "delete"),
                 ("Вложение добавлено", "attach"), ("Вложение убрано", "detach"),
                 ("Импорт из 1С", "import"), ("Выгрузка локальной базы", "upload"),
                 ("Сохранение", "save"))

# Изменение суммы на столько и больше подсвечивается в журнале.
AMOUNT_ALERT = 0.20

# Названия полей для журнала. Без них в таблице стояло бы «pay_date».
FIELD_TITLES = {
    "pay_date": "дата оплаты",
    "status": "статус",
    "comment": "комментарий",
    "supplier_id": "поставщик",
    "amount": "сумма",
    "priority": "приоритет",
    "dds_item": "статья ДДС",
    "title": "название",
    "paid_flag": "отметка об оплате",
    "recipient": "получатель",
    "vat": "НДС",
    "instead_of": "вместо",
    "responsible": "ответственный",
    "doc_number": "документ",
    "file": "файл",
    "touched": "оплаты",
    "ids": "записей",
    "note": "примечание",
    "color": "цвет",
    # Что стёрло удаление данных: журнал остаётся, и объём удаления виден в нём.
    "payments": "оплат",
    "files": "вложений",
    "budgets": "бюджетов",
    "imports": "импортов",
    "links": "привязок",
}


_MONEY_FIELDS = frozenset({"amount", "vat"})


def _show(name: str, value: Any) -> str:
    """Значение поля так, как его читает человек, а не как лежит в базе."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (list, tuple)):
        return "; ".join(str(item) for item in value)
    if name == "status":
        try:
            return STATUS_TITLES[PaymentStatus(value)]
        except (KeyError, ValueError):
            return str(value)
    if name in _MONEY_FIELDS:
        try:
            text = f"{float(value):,.2f}".replace(",", " ").removesuffix(".00")
            return text.replace(".", ",")
        except (TypeError, ValueError):
            return str(value)
    if name == "pay_date":
        try:
            return date.fromisoformat(str(value)[:10]).strftime("%d.%m.%Y")
        except ValueError:
            return str(value)
    return str(value)


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
        created_at=_moment(row.get("created_at")),
        ai_limit=None if row.get("ai_limit") is None else int(row["ai_limit"]),
        ai_used=int(row.get("ai_used") or 0),
        app_version=str(row.get("app_version") or ""),
        app_platform=str(row.get("app_platform") or ""),
        app_seen_at=_moment(row.get("app_seen_at")) if row.get("app_seen_at") else None)


def version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


def update_summary(accounts: list[Account], current: str) -> tuple[str, list[Account]]:
    """Кто ещё не перешёл на версию `current` или новее.

    Считаются только действующие учётки. Пустая версия — у человека приложение
    старше той, что умеет себя называть, значит, оно точно не новее. Возвращает
    строку для показа и список тех, кто отстал.
    """
    active = [account for account in accounts if account.is_active]
    behind = [account for account in active
              if not account.app_version
              or version_key(account.app_version) < version_key(current)]
    done = len(active) - len(behind)
    text = f"Версия {current} или новее: {done} из {len(active)}"
    if behind:
        text += ". Ещё нет: " + ", ".join(account.title for account in behind[:6])
        if len(behind) > 6:
            text += f" и ещё {len(behind) - 6}"
    return text, behind


# --- учётные записи ------------------------------------------------------------

def accounts() -> list[Account]:
    return [_account(row) for row in transport.get("/api/users")]


def create(account: Account) -> tuple[Account, str]:
    """Заводит учётку. Возвращает её и пароль — он показывается один раз."""
    answer = transport.post("/api/users", {
        "login": account.login, "full_name": account.full_name,
        "responsible": account.responsible, "directions": account.directions,
        "denied_pages": account.denied_pages, "ai_limit": account.ai_limit,
        "is_admin": account.is_admin, "is_active": account.is_active})
    account.id = int(answer["id"])
    account.login = answer["login"]
    return account, answer["password"]


def save(account: Account) -> Account:
    return _account(transport.put(f"/api/users/{account.id}", {
        "login": account.login, "full_name": account.full_name,
        "responsible": account.responsible, "directions": account.directions,
        "denied_pages": account.denied_pages, "ai_limit": account.ai_limit,
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

def journal(limit: int = 200, entity: str = "", login: str = "", *,
            record_id: int = 0, action: str = "", start: date | None = None,
            end: date | None = None, text: str = "") -> list[Entry]:
    """Журнал с отбором. Пустое условие выборку не ограничивает.

    Сервер прежней версии лишних условий не знает и игнорирует: журнал
    остаётся читаемым, просто неотфильтрованным.
    """
    rows = transport.get("/api/audit", {
        "limit": limit, "entity": entity, "login": login,
        "record_id": record_id or "", "action": action, "text": text.strip(),
        "start": start.isoformat() if start else "",
        "end": end.isoformat() if end else ""})
    return [
        Entry(id=row["id"], at=_moment(row["at"]), user=row["user"],
              entity=row["entity"], entity_id=row["entity_id"],
              action=row["action"], subject=row.get("subject") or "",
              kind=row.get("kind") or "", no_supplier=bool(row.get("no_supplier")),
              changes=row.get("changes") or {})
        for row in rows
    ]
