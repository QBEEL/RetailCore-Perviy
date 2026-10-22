"""Что лежит в отчёте 1С по брендам: товары, склады и остатки по ним."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Place:
    """Склад или магазин: остатки и продажи товара именно здесь."""

    name: str
    stock: float = 0.0
    sales: float = 0.0
    reserve: float = 0.0
    transit: float = 0.0
    free: float = 0.0


@dataclass(slots=True)
class Item:
    """Товар из отчёта: итоги по сети и разбивка по складам.

    Продажи — за период выгрузки, а он в файле не записан: 1С держит его в
    параметрах отчёта, которые в таблицу не попадают. Месячные продажи и запас
    в месяцах считаются там, где период известен, — в `analysis`.
    """

    brand: str
    category: str = ""
    article: str = ""
    name: str = ""
    trait: str = ""
    barcode: str = ""
    status: str = ""
    stock: float = 0.0
    sales: float = 0.0
    amount: float = 0.0
    # Что рекомендует сама 1С. Показывается рядом: это «продажи минус остаток»,
    # то есть заказ на весь период выгрузки, а не на нужный срок.
    recommended: float | None = None
    places: list[Place] = field(default_factory=list)
    row: int = 0

    @property
    def transit(self) -> float:
        return sum(place.transit for place in self.places)

    @property
    def key(self) -> str:
        """Чем товар отличается от других: артикул, затем штрихкод, затем название."""
        return self.article or self.barcode or f"{self.name}|{self.trait}"

    @property
    def title(self) -> str:
        return f"{self.name} · {self.trait}" if self.trait else self.name


@dataclass(slots=True)
class Stock:
    """Разобранный отчёт."""

    items: list[Item] = field(default_factory=list)
    # Склады и магазины в том порядке, в каком они стоят в шапке.
    places: list[str] = field(default_factory=list)
    # Склады, у которых в отчёте есть колонка «Продажи». У основного склада её
    # нет: он торгует не с полки, а отгружает магазинам.
    selling: list[str] = field(default_factory=list)
    sheet: str = ""
    source: str = ""
    warnings: list[str] = field(default_factory=list)
    # Сошлись ли товары с итогами брендов и складов самого файла. None — итогов нет.
    balanced: bool | None = None

    @property
    def brands(self) -> list[str]:
        seen: dict[str, None] = {}
        for item in self.items:
            seen.setdefault(item.brand, None)
        return list(seen)
