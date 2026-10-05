"""Модели оплат: платёж, статус, бюджет месяца, день календаря.

Про статусы стоит сказать отдельно. Выгрузка 1С описывает заявку двумя полями —
согласование («К оплате», «Отклонена», «Не согласована») и факт оплаты
(«Оплачена / Закрыта»). Пять статусов приложения складываются из их пары, а
исходные значения хранятся рядом: без них нельзя понять, почему заявка не
оплачена — её отклонили или она ещё висит на согласовании.

Просроченным делается только запланированный платёж. Оплаченный и отменённый не
пересчитываются никогда: иначе все отклонённые заявки прошлых лет при первом же
открытии стали бы просрочкой и повисли в дашборде вечным долгом.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class PaymentStatus(str, Enum):
    """Состояние платежа в приложении."""

    PLANNED = "planned"
    PAID = "paid"
    OVERDUE = "overdue"
    MOVED = "moved"
    CANCELLED = "cancelled"

    @property
    def title(self) -> str:
        return STATUS_TITLES[self]

    @property
    def open(self) -> bool:
        """Ждёт денег: попадает в «осталось оплатить» и в расчёт бюджета."""
        return self in (PaymentStatus.PLANNED, PaymentStatus.OVERDUE, PaymentStatus.MOVED)


STATUS_TITLES: dict[PaymentStatus, str] = {
    PaymentStatus.PLANNED: "Запланировано",
    PaymentStatus.PAID: "Оплачено",
    PaymentStatus.OVERDUE: "Просрочено",
    PaymentStatus.MOVED: "Перенесено",
    PaymentStatus.CANCELLED: "Отменено",
}

# Порядок для списков и фильтров: сначала то, что требует внимания.
STATUS_ORDER: tuple[PaymentStatus, ...] = (
    PaymentStatus.OVERDUE,
    PaymentStatus.PLANNED,
    PaymentStatus.MOVED,
    PaymentStatus.PAID,
    PaymentStatus.CANCELLED,
)


class PaymentOrigin(str, Enum):
    """Откуда взялась запись — от этого зависит, что вправе затирать импорт."""

    IMPORT = "import"
    MANUAL = "manual"
    ORDER = "order"
    PRICING = "pricing"
    PLAN = "plan"

    @property
    def title(self) -> str:
        return ORIGIN_TITLES[self]


ORIGIN_TITLES: dict[PaymentOrigin, str] = {
    PaymentOrigin.IMPORT: "Импорт из 1С",
    PaymentOrigin.MANUAL: "Создано вручную",
    PaymentOrigin.ORDER: "Из заказа",
    PaymentOrigin.PRICING: "Из переоценки",
    PaymentOrigin.PLAN: "План из Excel",
}

# Хозяйственная операция, по которой считается бюджет. Остальные виды (налоги,
# аренда, зарплата) в выгрузке есть, но к закупке отношения не имеют.
SUPPLIER_OPERATION = "Оплата поставщику"

# Копейки: суммы приходят строкой «1 649 018,00», сравнивать их на равенство
# после float-разбора нельзя.
AMOUNT_EPSILON = 0.005

# Под этим именем в пометке о смене суммы значится выгрузка 1С: сумму изменил
# не человек, а заявка, пришедшая на место плана или поправившая прежнюю.
IMPORT_AUTHOR = "1С"


def money_text(value: float) -> str:
    """«120 000» — сумма без копеек, тысячи через пробел."""
    return f"{abs(value):,.0f}".replace(",", " ")


@dataclass(slots=True)
class Payment:
    """Платёж — заявка из 1С либо созданная в приложении запись."""

    amount: float = 0.0
    pay_date: date | None = None
    status: PaymentStatus = PaymentStatus.PLANNED
    recipient: str = ""
    supplier_id: int = 0
    id: int = 0
    doc_number: str = ""
    request_date: date | None = None
    vat: float = 0.0
    currency: str = "руб."
    source_status: str = ""
    paid_flag: bool = False
    operation: str = SUPPLIER_OPERATION
    over_limit: bool = False
    priority: str = ""
    edo_state: str = ""
    responsible: str = ""
    author: str = ""
    comment: str = ""
    had_files: bool = False
    origin: PaymentOrigin = PaymentOrigin.MANUAL
    origin_ref: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Заполняется при чтении списка — только для показа.
    files: int = 0
    supplier_name: str = ""
    # Смена суммы — рукой или выгрузкой 1С (тогда «кто» — IMPORT_AUTHOR):
    # сумма до первой правки, кто и когда правил. Ставит
    # хранилище при сохранении, а не карточка — иначе пометку можно было бы
    # выставить или стереть, просто пересохранив запись.
    amount_before: float | None = None
    amount_changed_by: str = ""
    amount_changed_at: datetime | None = None

    @property
    def amount_changed(self) -> bool:
        """Сумму поменяли, и она не совпадает с прежней."""
        return (self.amount_before is not None
                and abs(self.amount_before - self.amount) >= AMOUNT_EPSILON)

    @property
    def amount_by_import(self) -> bool:
        """Сумму поменяла выгрузка 1С, а не человек."""
        return self.amount_changed and self.amount_changed_by == IMPORT_AUTHOR

    @property
    def amount_delta(self) -> float:
        """На сколько сумма ушла от прежней: плюс — выросла, минус — снизилась."""
        if not self.amount_changed:
            return 0.0
        return self.amount - (self.amount_before or 0.0)

    @property
    def amount_direction(self) -> str:
        """«▲ +55 876 ₽» или «▼ −20 000 ₽» — в какую сторону ушла сумма."""
        delta = self.amount_delta
        if not delta:
            return ""
        return f"▲ +{money_text(delta)} ₽" if delta > 0 else f"▼ −{money_text(delta)} ₽"

    @property
    def amount_change_text(self) -> str:
        """«было 120 000 ₽ ▲ +5 000 ₽ · 1С, 25.09» — для подсказок и списка дня."""
        if not self.amount_changed:
            return ""
        who = ", ".join(part for part in (
            self.amount_changed_by,
            f"{self.amount_changed_at:%d.%m}" if self.amount_changed_at else "") if part)
        return (f"было {money_text(self.amount_before or 0.0)} ₽ {self.amount_direction}"
                + (f" · {who}" if who else ""))

    @property
    def key(self) -> tuple[str, str]:
        """Ключ дедупликации: номер заявки обнуляется каждый год, дата его различает."""
        return (self.doc_number, self.request_date.isoformat() if self.request_date else "")

    @property
    def title(self) -> str:
        return self.supplier_name or self.recipient or "без получателя"

    @property
    def counts_to_budget(self) -> bool:
        """В бюджет месяца идут только оплаты поставщикам, не налоги и аренда."""
        return self.operation == SUPPLIER_OPERATION and self.status is not PaymentStatus.CANCELLED

    @property
    def editable_by_import(self) -> bool:
        """Можно ли обновлять запись данными из 1С.

        Созданное в приложении импорт не трогает: у такой записи нет пары
        «номер + дата заявки» из выгрузки, а совпадение по ней было бы случайным.
        """
        return self.origin is PaymentOrigin.IMPORT

    def resolved_status(self, today: date | None = None) -> PaymentStatus:
        """Статус с учётом ушедшей даты. Меняет только «Запланировано»."""
        if self.status is not PaymentStatus.PLANNED:
            return self.status
        if self.pay_date is None:
            return self.status
        return PaymentStatus.OVERDUE if self.pay_date < (today or date.today()) else self.status


@dataclass(slots=True)
class PaymentFile:
    """Документ, приложенный к платежу в приложении.

    Файл копируется в папку профиля: путь к исходнику ломается, как только его
    переложат или пришлют новую версию письмом.
    """

    name: str = ""
    path: str = ""
    size: int = 0
    id: int = 0
    payment_id: int = 0
    added_at: datetime | None = None

    @property
    def exists(self) -> bool:
        return bool(self.path) and os.path.isfile(self.path)


@dataclass(slots=True)
class SupplierRow:
    """Поставщик, каким его видно из оплат.

    Карточки в базе поставщиков заводятся руками и есть далеко не у всех, а
    получатель приходит с каждой выгрузкой 1С. Поэтому настоящий список
    поставщиков — этот.
    """

    recipient_key: str = ""
    recipient: str = ""
    supplier_id: int = 0
    payments: int = 0
    amount: float = 0.0
    last_pay: date | None = None
    # Все, кто платил, от частого к редкому.
    #
    # Список, а не одно имя, но причина не та, что казалась. Считалось, что
    # почти половина получателей оплачивается несколькими менеджерами; на деле
    # столько их только потому, что в оплатах видны и те, кто поставщиков не
    # ведёт, — офис-менеджер и бухгалтерия проводят платежи по чужим заявкам.
    # Среди категорийных менеджеров общий поставщик — редкость: на 148
    # размеченных таких оказалось два.
    #
    # Кто ведёт поставщика, здесь не отвечают вовсе: это `supplier_assignment`
    # на сервере. Тут только факт оплаты.
    managers: list[str] = field(default_factory=list)

    @property
    def main_manager(self) -> str:
        return self.managers[0] if self.managers else ""

    @property
    def shared(self) -> bool:
        """С поставщиком работает больше одного человека."""
        return len(self.managers) > 1

    @property
    def manager_title(self) -> str:
        if not self.managers:
            return ""
        if len(self.managers) == 1:
            return self.managers[0]
        return f"{self.managers[0]} + ещё {len(self.managers) - 1}"

    @property
    def has_card(self) -> bool:
        return bool(self.supplier_id)


@dataclass(slots=True)
class Budget:
    """Бюджет месяца."""

    year: int = 0
    month: int = 0
    amount: float = 0.0
    note: str = ""
    updated_at: datetime | None = None

    @property
    def period(self) -> tuple[int, int]:
        return (self.year, self.month)

    @property
    def title(self) -> str:
        return f"{MONTHS[self.month - 1]} {self.year}"


MONTHS: tuple[str, ...] = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)
MONTHS_OF: tuple[str, ...] = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS: tuple[str, ...] = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


@dataclass(slots=True)
class BudgetUse:
    """Исполнение бюджета месяца."""

    budget: Budget
    spent: float = 0.0
    planned: float = 0.0
    count: int = 0

    @property
    def total(self) -> float:
        """Оплачено плюс то, что ещё предстоит: именно это сравнивается с бюджетом."""
        return self.spent + self.planned

    @property
    def left(self) -> float:
        return self.budget.amount - self.total

    @property
    def percent(self) -> float:
        if self.budget.amount <= 0:
            return 0.0
        return self.total / self.budget.amount * 100.0

    @property
    def over(self) -> bool:
        return self.budget.amount > 0 and self.total > self.budget.amount + AMOUNT_EPSILON

    def near(self, threshold: float) -> bool:
        """Подходит к пределу: предупреждать до превышения, а не после."""
        return not self.over and self.budget.amount > 0 and self.percent >= threshold


# Пороги суммы за день для цветовой индикации календаря.
#
# Значения по умолчанию выбраны по истории: медиана дня с оплатами — 1,43 млн,
# 75-й процентиль — 2,8 млн. На шкале 100/300/700 тысяч, которая кажется
# естественной, красными выходят 70 % дней и цвет перестаёт что-либо значить.
# Здешние пороги делят историю примерно на четыре равные части.
DEFAULT_DAY_LEVELS: tuple[float, float, float] = (500_000.0, 1_500_000.0, 3_000_000.0)

# Готовые наборы порогов: по истории и мелкими суммами, если оборот другой.
LEVEL_PRESETS: dict[str, tuple[float, float, float]] = {
    "По истории (500 тыс · 1,5 млн · 3 млн)": DEFAULT_DAY_LEVELS,
    "Мелкие суммы (100 · 300 · 700 тыс)": (100_000.0, 300_000.0, 700_000.0),
    "Крупные суммы (1 · 3 · 7 млн)": (1_000_000.0, 3_000_000.0, 7_000_000.0),
}


class DayLevel(int, Enum):
    """Насколько тяжёлый день по сумме оплат."""

    EMPTY = 0
    LIGHT = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @property
    def title(self) -> str:
        return LEVEL_TITLES[self]


LEVEL_TITLES: dict[DayLevel, str] = {
    DayLevel.EMPTY: "без оплат",
    DayLevel.LIGHT: "небольшая нагрузка",
    DayLevel.MEDIUM: "средняя нагрузка",
    DayLevel.HIGH: "высокая нагрузка",
    DayLevel.CRITICAL: "критическая нагрузка",
}


def level_of(amount: float, levels: tuple[float, float, float] = DEFAULT_DAY_LEVELS) -> DayLevel:
    """Уровень дня по сумме. Пороги задаются пользователем в настройках."""
    if amount <= 0:
        return DayLevel.EMPTY
    low, middle, high = levels
    if amount <= low:
        return DayLevel.LIGHT
    if amount <= middle:
        return DayLevel.MEDIUM
    if amount <= high:
        return DayLevel.HIGH
    return DayLevel.CRITICAL


@dataclass(slots=True)
class Day:
    """День календаря: что в нём оплачивается и на какую сумму."""

    day: date
    payments: list[Payment] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(p.amount for p in self.payments if p.status is not PaymentStatus.CANCELLED)

    @property
    def count(self) -> int:
        return len(self.payments)

    @property
    def overdue(self) -> int:
        return sum(1 for p in self.payments if p.status is PaymentStatus.OVERDUE)

    @property
    def amount_changed(self) -> int:
        """Сколько оплат дня с изменённой суммой — рукой или выгрузкой 1С."""
        return sum(1 for p in self.payments if p.amount_changed)

    @property
    def weekend(self) -> bool:
        return self.day.weekday() >= 5

    def level(self, levels: tuple[float, float, float] = DEFAULT_DAY_LEVELS) -> DayLevel:
        return level_of(self.total, levels)


@dataclass(slots=True)
class Stats:
    """Показатели по выборке платежей."""

    total: float = 0.0
    count: int = 0
    paid: float = 0.0
    paid_count: int = 0
    planned: float = 0.0
    planned_count: int = 0
    overdue: float = 0.0
    overdue_count: int = 0
    minimum: float = 0.0
    maximum: float = 0.0
    biggest: Payment | None = None
    busiest_day: Day | None = None

    @property
    def average(self) -> float:
        return self.total / self.count if self.count else 0.0


# Недобор к плану, после которого строка считается выбившейся. Двадцать
# процентов — не свойство данных, а договорённость отдела: меньше списывают на
# округление сумм и перенос оплаты через край месяца.
PLAN_DEVIATION_LIMIT = 20.0


@dataclass(slots=True)
class PlanFact:
    """План и факт по одному поставщику за месяц.

    План — всё намеченное на месяц, независимо от того, как запись появилась:
    присланный Excel, заведённое вручную, вышедшее из заказа и переоценки,
    заявка из 1С. Счёт, набитый в 1С и стоящий на конкретное число, запланирован
    ровно так же, как строка из Excel, — по источнику их не делят.

    Факт — оплаченное из этого же. Оплата не заводит новую запись, а меняет
    статус существующей, поэтому факт всегда часть плана: оплатить больше, чем
    намечено, нельзя — можно только наметить ещё.

    Обе величины считаются по правилу `counts_to_budget`: отменённое и
    непоставщические операции не в счёт, иначе недобор объяснялся бы не работой
    менеджера, а налогами и арендой.
    """

    recipient: str = ""
    supplier_id: int = 0
    planned: float = 0.0
    actual: float = 0.0
    planned_count: int = 0
    actual_count: int = 0

    @property
    def empty(self) -> bool:
        """Ни плана, ни оплаты — показывать нечего."""
        return not (self.planned or self.actual)

    @property
    def title(self) -> str:
        return self.recipient or "без получателя"

    @property
    def left(self) -> float:
        """Сколько из намеченного ещё не оплачено."""
        return max(self.planned - self.actual, 0.0)

    @property
    def done_share(self) -> float | None:
        """Исполнение плана в процентах.

        `None`, когда плана не было вовсе: делить не на что. Оплаты без плана
        при этом не бывает — запись сначала намечают, потом оплачивают.
        """
        if not self.planned:
            return None
        return self.actual / self.planned * 100.0

    @property
    def off_plan(self) -> bool:
        """Недобрали больше положенного — строка должна быть заметна."""
        share = self.done_share
        return share is not None and share < 100.0 - PLAN_DEVIATION_LIMIT


@dataclass(slots=True)
class SupplierStats:
    """История оплат одного получателя — основа рейтинга и предложений."""

    recipient: str = ""
    supplier_id: int = 0
    total: float = 0.0
    count: int = 0
    minimum: float = 0.0
    maximum: float = 0.0
    median_amount: float = 0.0
    first_pay: date | None = None
    last_pay: date | None = None
    median_interval: float = 0.0
    median_terms: float = 0.0
    common_day: int = 0
    day_share: float = 0.0

    @property
    def average(self) -> float:
        return self.total / self.count if self.count else 0.0

    @property
    def title(self) -> str:
        return self.recipient or "без получателя"

    def silent_days(self, today: date | None = None) -> int:
        """Сколько дней прошло с последней оплаты."""
        if self.last_pay is None:
            return 0
        return max(((today or date.today()) - self.last_pay).days, 0)


@dataclass(slots=True)
class Period:
    """Сумма за отрезок — месяц, год или день. Для графиков и динамики."""

    label: str
    start: date
    total: float = 0.0
    count: int = 0
    previous: float = 0.0

    @property
    def change(self) -> float:
        """Прирост к предыдущему отрезку в процентах."""
        if self.previous <= 0:
            return 0.0
        return (self.total - self.previous) / self.previous * 100.0


class SuggestionKind(str, Enum):
    """Что именно подсказала история."""

    REGULAR = "regular"
    SILENT = "silent"

    @property
    def title(self) -> str:
        return "Регулярный платёж" if self is SuggestionKind.REGULAR else "Давно не оплачивался"


@dataclass(slots=True)
class Suggestion:
    """Предложение создать оплату. Ничего не создаёт само — только подсказывает."""

    kind: SuggestionKind
    stats: SupplierStats
    pay_date: date | None = None
    amount: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return " · ".join(self.reasons)

    @property
    def urgent(self) -> bool:
        return self.kind is SuggestionKind.SILENT


@dataclass(slots=True)
class RowChange:
    """Что импорт сделает с одной заявкой — строка предпросмотра.

    Итоги («изменится 33») не отвечают на главный вопрос: какие именно заявки и
    как. Эта запись хранит до и после по каждой, чтобы их можно было просмотреть
    до записи и сохранить в Excel после.
    """

    kind: str                      # new · plan · changed
    doc_number: str
    request_date: date | None
    recipient: str
    amount_after: float
    # Сумма в базе до импорта; у новой заявки её нет.
    amount_before: float | None = None
    # Прочие поля: (название, было, стало).
    fields: list[tuple[str, str, str]] = field(default_factory=list)
    note: str = ""
    # Ключ заявки «номер + дата заявки» — по нему правка суммы находит строку.
    key: tuple[str, str] = ("", "")
    # Сумма, которую предложила программа, если человек её поправил до записи.
    amount_proposed: float | None = None

    KINDS = {"new": "Новая", "plan": "Заменит план", "changed": "Изменится"}

    @property
    def edited(self) -> bool:
        return self.amount_proposed is not None

    @property
    def kind_title(self) -> str:
        return self.KINDS.get(self.kind, self.kind)

    @property
    def delta(self) -> float:
        if self.amount_before is None:
            return 0.0
        change = self.amount_after - self.amount_before
        return change if abs(change) >= AMOUNT_EPSILON else 0.0

    @property
    def direction(self) -> str:
        """«▲ +55 876 ₽» — в какую сторону уйдёт сумма; у новой пусто."""
        if not self.delta:
            return ""
        sign, arrow = ("+", "▲") if self.delta > 0 else ("−", "▼")
        return f"{arrow} {sign}{money_text(self.delta)} ₽"

    @property
    def fields_text(self) -> str:
        parts = [f"{name}: {before or '—'} → {after or '—'}"
                 for name, before, after in self.fields]
        if self.note:
            parts.insert(0, self.note)
        if self.edited:
            parts.insert(0, f"сумма поправлена вручную, предлагалось {money_text(self.amount_proposed)} ₽")
        return "; ".join(parts)


@dataclass(slots=True)
class ImportReport:
    """Итог разбора выгрузки — показывается до записи в базу."""

    path: str = ""
    rows: int = 0
    new: int = 0
    updated: int = 0
    # Новые заявки 1С, которые встанут на место уже заведённой ручной или
    # плановой оплаты, а не лягут рядом с ней второй записью.
    adopted: int = 0
    same: int = 0
    # Куда уйдут суммы уже лежащих в базе записей — и изменившихся, и занявших
    # место плана: сколько выросло, сколько снизилось и на какие деньги.
    raised: int = 0
    lowered: int = 0
    raised_sum: float = 0.0
    lowered_sum: float = 0.0
    # Каждая заявка, которую импорт создаст или изменит, — до и после.
    details: list[RowChange] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    payments: list[Payment] = field(default_factory=list)
    first_pay: date | None = None
    last_pay: date | None = None
    recipients: int = 0
    applied: bool = False
    # Суммы, поправленные человеком в предпросмотре: ключ заявки → сумма. В
    # `payments` лежит то, что прислала 1С, — правка накладывается при записи,
    # уже после подбора пар (`importer.split_changes`), чтобы не менять пары.
    overrides: dict[tuple[str, str], float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return sum(self.overrides.get(p.key, p.amount) for p in self.payments)

    def set_amount(self, key: tuple[str, str], value: float) -> bool:
        """Правит сумму заявки в предпросмотре. Возвращает, нашлась ли строка."""
        row = next((r for r in self.details if r.key == key), None)
        if row is None or value <= 0:
            return False
        proposed = row.amount_proposed if row.edited else row.amount_after
        if abs(value - proposed) < AMOUNT_EPSILON:
            # Вернули предложенное — правки нет.
            self.overrides.pop(key, None)
            row.amount_proposed = None
        else:
            self.overrides[key] = float(value)
            row.amount_proposed = proposed
        row.amount_after = float(value) if row.edited else proposed
        self.recount_amounts()
        return True

    def recount_amounts(self) -> None:
        """Пересчитывает «выросла / снизилась» по строкам списка."""
        self.raised = self.lowered = 0
        self.raised_sum = self.lowered_sum = 0.0
        for row in self.details:
            if row.amount_before is not None:
                self.note_amount(row.amount_before, row.amount_after)

    @property
    def changes(self) -> int:
        return self.new + self.updated + self.adopted

    def note_amount(self, before: float, after: float) -> None:
        """Учитывает смену суммы существующей записи: куда и на сколько."""
        delta = after - before
        if abs(delta) < AMOUNT_EPSILON:
            return
        if delta > 0:
            self.raised += 1
            self.raised_sum += delta
        else:
            self.lowered += 1
            self.lowered_sum -= delta

    @property
    def direction(self) -> str:
        """«выросла у 12 (+340 000 ₽) · снизилась у 5 (−120 000 ₽)» — или пусто."""
        parts = []
        if self.raised:
            parts.append(f"выросла у {self.raised} (+{money_text(self.raised_sum)} ₽)")
        if self.lowered:
            parts.append(f"снизилась у {self.lowered} (−{money_text(self.lowered_sum)} ₽)")
        return " · ".join(parts)

    @property
    def summary(self) -> str:
        parts = [f"прочитано {self.rows}"]
        if self.new:
            parts.append(f"новых {self.new}")
        if self.adopted:
            parts.append(f"заменят план {self.adopted}")
        if self.updated:
            parts.append(f"изменилось {self.updated}")
        if self.direction:
            parts.append(f"сумма {self.direction}")
        if self.same:
            parts.append(f"без изменений {self.same}")
        if self.skipped:
            parts.append(f"пропущено {len(self.skipped)}")
        return " · ".join(parts)
