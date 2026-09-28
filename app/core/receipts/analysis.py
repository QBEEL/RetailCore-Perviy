"""Выводы по чекам: выручка, прибыль, средний чек и срезы.

Чек — это документ, а не строка: в одном чеке несколько товаров, и средний
чек по строкам вышел бы средней ценой товара. Поэтому чеки везде считаются по
номеру документа.

Средний чек и число товаров в чеке считаются только по продажам. Возврат —
тоже документ, но включить его в число чеков значило бы занизить средний чек
ровно на столько, сколько вернули. В выручку возвраты входят со своим минусом:
выручка должна сходиться с «Суммой» файла.

Скидка показывается долей от суммы до скидки: «10 % скидок» понятнее, чем
отношение скидки к уже уменьшенной выручке.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable

from .models import Line, Receipts

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
SELF_SERVICE_TITLE = "Самостоятельная покупка"
NO_VALUE = "не указано"


@dataclass(slots=True)
class Group:
    """Строка среза: магазин, бренд, товар, продавец, день."""

    name: str = ""
    revenue: float = 0.0
    cost: float = 0.0
    quantity: float = 0.0
    discount: float = 0.0
    bonus_accrued: float = 0.0
    returned: float = 0.0
    lines: int = 0
    documents: set[str] = field(default_factory=set)
    sales: set[str] = field(default_factory=set)
    sale_revenue: float = 0.0
    sale_quantity: float = 0.0
    stores: set[str] = field(default_factory=set)
    # Для товара — бренд и категория, для остальных пусто.
    brand: str = ""
    category: str = ""

    def add(self, line: Line) -> None:
        self.revenue += line.amount
        self.cost += line.cost
        self.quantity += line.quantity
        self.discount += line.discount
        self.bonus_accrued += line.bonus_accrued
        self.lines += 1
        self.documents.add(line.document)
        if line.store:
            self.stores.add(line.store)
        if line.is_return:
            self.returned += -line.amount
        else:
            self.sales.add(line.document)
            self.sale_revenue += line.amount
            self.sale_quantity += line.quantity

    @property
    def checks(self) -> int:
        return len(self.sales)

    @property
    def profit(self) -> float:
        return self.revenue - self.cost

    @property
    def margin(self) -> float:
        return self.profit / self.revenue if self.revenue else 0.0

    @property
    def average_check(self) -> float:
        return self.sale_revenue / self.checks if self.checks else 0.0

    @property
    def per_check(self) -> float:
        return self.sale_quantity / self.checks if self.checks else 0.0

    @property
    def discount_share(self) -> float:
        before = self.revenue + self.discount
        return self.discount / before if before else 0.0


@dataclass(slots=True)
class Report:
    receipts: Receipts
    total: Group
    stores: list[Group]
    brands: list[Group]
    categories: list[Group]
    items: list[Group]
    sellers: list[Group]
    days: list[tuple[date, Group]]
    hours: list[tuple[int, Group]]
    promos: list[Group]
    kinds: list[Group]
    returns: list[Line]
    guests: int = 0
    repeat_guests: int = 0
    self_service: Group = field(default_factory=Group)

    @property
    def per_day(self) -> float:
        days = self.receipts.days
        return self.total.revenue / days if days else 0.0

    def share(self, group: Group) -> float:
        return group.revenue / self.total.revenue if self.total.revenue else 0.0


def build(receipts: Receipts) -> Report:
    lines = receipts.lines
    total = Group(name="Итого")
    for line in lines:
        total.add(line)

    items = _groups(lines, lambda line: line.item)
    by_item: dict[str, Line] = {}
    for line in lines:
        by_item.setdefault(line.item, line)
    for group in items:
        sample = by_item.get(group.name)
        if sample is not None:
            group.brand, group.category = sample.brand, sample.category

    sellers = _groups([line for line in lines if not line.self_service],
                      lambda line: line.seller)
    self_service = Group(name=SELF_SERVICE_TITLE)
    for line in lines:
        if line.self_service:
            self_service.add(line)

    days = sorted(_by(lines, lambda line: line.day).items())
    # Часы — только продажи: срез отвечает на вопрос «когда покупают», и час,
    # в котором был лишь возврат, встал бы в него строкой с нулём чеков. Дни
    # считаются со всем подряд — их выручка должна сходиться с итогом.
    hours = sorted(_by([line for line in lines if line.moment and not line.is_return],
                       lambda line: line.moment.hour).items())

    visits: dict[str, set[str]] = defaultdict(set)
    for line in lines:
        if line.known_guest and not line.is_return:
            visits[line.guest.strip().lower()].add(line.document)

    return Report(
        receipts=receipts,
        total=total,
        stores=_groups(lines, lambda line: line.store),
        brands=_groups(lines, lambda line: line.brand),
        categories=_groups(lines, lambda line: line.category),
        items=items,
        sellers=sellers,
        days=[(day, group) for day, group in days if day is not None],
        hours=hours,
        promos=_groups([line for line in lines if line.promo], lambda line: line.promo),
        kinds=_groups(lines, lambda line: line.kind),
        returns=sorted((line for line in lines if line.is_return),
                       key=lambda line: (line.day or date.min, line.document)),
        guests=len(visits),
        repeat_guests=sum(1 for documents in visits.values() if len(documents) > 1),
        self_service=self_service,
    )


def _by(lines: Iterable[Line], key: Callable[[Line], object]) -> dict:
    groups: dict = {}
    for line in lines:
        name = key(line)
        group = groups.get(name)
        if group is None:
            group = groups[name] = Group(name=str(name) if name not in (None, "") else NO_VALUE)
        group.add(line)
    return groups


def _groups(lines: Iterable[Line], key: Callable[[Line], str]) -> list[Group]:
    """Срез по ключу, от большей выручки к меньшей."""
    return sorted(_by(lines, lambda line: key(line) or NO_VALUE).values(),
                  key=lambda group: (-group.revenue, group.name))


def weekday(day: date) -> str:
    return WEEKDAYS[day.weekday()]
