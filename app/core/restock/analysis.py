"""Сколько хватит товара и сколько заказать: расчёт по продажам за период.

Период выгрузки в самом отчёте не записан, и задаёт его человек. Остальное
берётся из файла: продажи, остаток и товар в пути по сети и по каждому складу.

Запас считается в месяцах — так его считали руками, делением продаж на число
месяцев. Позиция — это остаток плюс то, что уже едет: заказывать то, что в
пути, второй раз незачем. Резерв не вычитается: это те же штуки на полке, и
вычитать его значило бы заказывать больше, чем уходит.

Чего расчёт не знает, и поэтому говорит об этом, а не выдумывает:
- продажи считаются за весь период, а не за дни, когда товар был в наличии. Если
  товар закончился в середине периода, его спрос занижен;
- товар без продаж может быть свежим приходом, а не «неликвидом»: даты прихода
  в отчёте нет;
- кратность упаковки и минимальная партия поставщика неизвестны, заказ
  округляется только до штуки.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from .models import Item, Stock

# Средний месяц в днях. Месяцев в периоде считается столько же, сколько
# получилось бы у человека, поделившего число дней на 30,4.
AVG_MONTH = 365.25 / 12

OUT = "Нет в наличии"
LOW = "Мало"
OK = "Норма"
HIGH = "Избыток"
IDLE = "Нет продаж"
DEAD = "Нет движения"

# От срочного к спокойному: так позиции и идут в таблице.
STATES = (OUT, LOW, OK, HIGH, IDLE, DEAD)

DEFAULT_TARGET = 2.0
DEFAULT_LOW = 1.0
DEFAULT_HIGH = 6.0


@dataclass(frozen=True, slots=True)
class Params:
    """Период выгрузки и сроки, по которым считается запас."""

    start: date
    end: date
    # На сколько месяцев продаж заказывать: заказ добирает позицию до этого срока.
    target: float = DEFAULT_TARGET
    # Запаса меньше этого срока — «Мало», больше — «Избыток».
    low: float = DEFAULT_LOW
    high: float = DEFAULT_HIGH

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def months(self) -> float:
        return self.days / AVG_MONTH

    @property
    def title(self) -> str:
        return f"{self.start:%d.%m.%Y} — {self.end:%d.%m.%Y}"

    def check(self) -> None:
        if self.end < self.start:
            raise ValueError("Конец периода раньше его начала")
        if self.target <= 0:
            raise ValueError("Срок заказа должен быть больше нуля")
        if not 0 < self.low < self.high:
            raise ValueError("Порог «Мало» должен быть меньше порога «Избыток»")
        if self.target > self.high:
            raise ValueError(
                "Срок заказа больше порога «Избыток»: заказанное сразу считалось бы избытком")


@dataclass(slots=True)
class Row:
    """Товар с расчётом."""

    item: Item
    per_month: float
    position: float
    cover: float | None
    state: str
    order: int
    # Количество, которое человек поправил руками; None — берётся расчётное.
    manual: int | None = None

    @property
    def qty(self) -> int:
        return self.order if self.manual is None else self.manual

    @property
    def changed(self) -> bool:
        return self.manual is not None and self.manual != self.order


@dataclass(slots=True)
class Group:
    """Итоги по бренду."""

    name: str
    items: int = 0
    stock: float = 0.0
    transit: float = 0.0
    sales: float = 0.0
    ordered_items: int = 0
    order: int = 0
    states: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class PlaceSummary:
    """Итоги по складу или магазину."""

    name: str
    selling: bool
    stock: float = 0.0
    transit: float = 0.0
    sales: float = 0.0
    sold_items: int = 0
    # Продажи были, а остатка нет: здесь товар кончился.
    missing_items: int = 0
    cover: float | None = None


@dataclass(slots=True)
class Plan:
    """Расчёт целиком."""

    params: Params
    stock: Stock
    rows: list[Row]
    places: list[PlaceSummary]
    warnings: list[str] = field(default_factory=list)

    def of_brand(self, brand: str | None) -> list[Row]:
        return self.rows if not brand else [row for row in self.rows if row.item.brand == brand]

    def brands(self) -> list[Group]:
        groups: dict[str, Group] = {}
        for row in self.rows:
            group = groups.setdefault(row.item.brand, Group(row.item.brand))
            group.items += 1
            group.stock += row.item.stock
            group.transit += row.item.transit
            group.sales += row.item.sales
            group.states[row.state] = group.states.get(row.state, 0) + 1
            if row.qty > 0:
                group.ordered_items += 1
                group.order += row.qty
        return list(groups.values())

    def counts(self, rows: list[Row] | None = None) -> dict[str, int]:
        result = {state: 0 for state in STATES}
        for row in self.rows if rows is None else rows:
            result[row.state] += 1
        return result

    def to_order(self, brand: str | None = None) -> list[Row]:
        """Позиции с ненулевым заказом — в порядке файла, по брендам."""
        return [row for row in self.of_brand(brand) if row.qty > 0]


def _state(sales: float, position: float, cover: float | None, params: Params) -> str:
    if sales <= 0:
        return IDLE if position > 0 else DEAD
    if position <= 0:
        return OUT
    assert cover is not None
    if cover < params.low:
        return LOW
    if cover > params.high:
        return HIGH
    return OK


def calculate(item: Item, params: Params) -> Row:
    per_month = item.sales / params.months if item.sales > 0 else 0.0
    position = item.stock + item.transit
    cover = max(position, 0.0) / per_month if per_month else None
    state = _state(item.sales, position, cover, params)
    order = 0
    if per_month:
        need = per_month * params.target - max(position, 0.0)
        # Дробная часть в пределах погрешности — не повод заказывать лишнюю штуку.
        order = max(0, math.ceil(need - 1e-9))
    return Row(item, per_month, position, cover, state, order)


def build(stock: Stock, params: Params) -> Plan:
    params.check()
    rows = [calculate(item, params) for item in stock.items]
    plan = Plan(params, stock, rows, _places(stock, params))
    negatives = sum(1 for item in stock.items if item.stock < 0)
    if negatives:
        plan.warnings.append(
            f"Отрицательный остаток у {negatives} товаров — это непроведённый приход или "
            "пересорт; при расчёте остаток таких товаров принят за ноль")
    if params.days < 28:
        plan.warnings.append(
            f"Период всего {params.days} дн.: продажи за такой срок случайны, месячные "
            "цифры и заказ будут неточными")
    lost = [row for row in rows if row.state == OUT]
    if lost:
        plan.warnings.append(
            f"У {len(lost)} товаров остатка нет, а продажи были: спрос в дни без товара "
            "не виден, поэтому заказ по ним скорее занижен")
    return plan


def _places(stock: Stock, params: Params) -> list[PlaceSummary]:
    summaries = {name: PlaceSummary(name, name in stock.selling) for name in stock.places}
    for item in stock.items:
        for place in item.places:
            summary = summaries.get(place.name)
            if summary is None:
                continue
            summary.stock += place.stock
            summary.transit += place.transit
            summary.sales += place.sales
            if place.sales > 0:
                summary.sold_items += 1
                if place.stock <= 0:
                    summary.missing_items += 1
    for summary in summaries.values():
        if summary.sales > 0:
            summary.cover = max(summary.stock, 0.0) / (summary.sales / params.months)
    return list(summaries.values())
