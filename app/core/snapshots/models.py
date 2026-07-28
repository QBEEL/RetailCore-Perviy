"""Модели истории данных: снимок выгрузки, товар в нём и различия версий."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Роли, которые хранятся отдельными колонками и участвуют в сравнении версий.
# Остальные значения строки уходят в payload целиком и не теряются.
TRACKED = ("article", "ean", "sku", "name", "brand", "category", "volume", "price")

# Поля, изменение которых показывается как «изменение характеристик».
COMPARED = ("name", "brand", "category", "volume")


@dataclass(slots=True)
class Snapshot:
    """Состояние файла на момент загрузки."""

    id: int
    created_at: datetime
    source_file_name: str
    source_file_path: str
    source_file_hash: str
    sheet_name: str
    total_products: int
    brand: str = ""
    category: str = ""
    user_id: str = ""
    description: str = ""

    @property
    def label(self) -> str:
        return f"{self.created_at:%d.%m.%Y %H:%M} · {self.source_file_name}"


@dataclass(slots=True)
class SnapshotProduct:
    """Товар внутри снимка: разобранные поля плюс исходная строка файла."""

    row: int
    article: str = ""
    ean: str = ""
    sku: str = ""
    name: str = ""
    brand: str = ""
    category: str = ""
    volume: str = ""
    price: float | None = None
    match_key: str = ""
    values: list[Any] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Ключ сопоставления версий: артикул → EAN → нормализованное название.

        Тот же порядок надёжности, что и при сопоставлении файлов: артикул
        уникален, штрихкод почти уникален, название — последняя попытка.
        """
        return self.article or self.ean or self.match_key or self.name

    @property
    def label(self) -> str:
        return self.name or self.article or self.ean or f"Строка {self.row}"


@dataclass(slots=True)
class ProductChange:
    """Изменение одного товара между двумя снимками."""

    before: SnapshotProduct
    after: SnapshotProduct
    fields: list[str] = field(default_factory=list)

    @property
    def price_changed(self) -> bool:
        return "price" in self.fields

    @property
    def price_delta(self) -> float | None:
        if self.before.price is None or self.after.price is None:
            return None
        return self.after.price - self.before.price


@dataclass(slots=True)
class SnapshotDiff:
    """Различия между снимками: что добавилось, ушло и изменилось."""

    added: list[SnapshotProduct] = field(default_factory=list)
    removed: list[SnapshotProduct] = field(default_factory=list)
    changed: list[ProductChange] = field(default_factory=list)

    @property
    def price_changes(self) -> list[ProductChange]:
        return [c for c in self.changed if c.price_changed]

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)
