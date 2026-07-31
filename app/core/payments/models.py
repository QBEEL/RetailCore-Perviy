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

    @property
    def title(self) -> str:
        return ORIGIN_TITLES[self]


ORIGIN_TITLES: dict[PaymentOrigin, str] = {
    PaymentOrigin.IMPORT: "Импорт из 1С",
    PaymentOrigin.MANUAL: "Создано вручную",
    PaymentOrigin.ORDER: "Из заказа",
    PaymentOrigin.PRICING: "Из переоценки",
}

# Хозяйственная операция, по которой считается бюджет. Остальные виды (налоги,
# аренда, зарплата) в выгрузке есть, но к закупке отношения не имеют.
SUPPLIER_OPERATION = "Оплата поставщику"

# Копейки: суммы приходят строкой «1 649 018,00», сравнивать их на равенство
# после float-разбора нельзя.
AMOUNT_EPSILON = 0.005


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
class ImportReport:
    """Итог разбора выгрузки — показывается до записи в базу."""

    path: str = ""
    rows: int = 0
    new: int = 0
    updated: int = 0
    same: int = 0
    skipped: list[str] = field(default_factory=list)
    payments: list[Payment] = field(default_factory=list)
    first_pay: date | None = None
    last_pay: date | None = None
    recipients: int = 0
    applied: bool = False

    @property
    def total(self) -> float:
        return sum(p.amount for p in self.payments)

    @property
    def changes(self) -> int:
        return self.new + self.updated

    @property
    def summary(self) -> str:
        parts = [f"прочитано {self.rows}"]
        if self.new:
            parts.append(f"новых {self.new}")
        if self.updated:
            parts.append(f"изменилось {self.updated}")
        if self.same:
            parts.append(f"без изменений {self.same}")
        if self.skipped:
            parts.append(f"пропущено {len(self.skipped)}")
        return " · ".join(parts)
