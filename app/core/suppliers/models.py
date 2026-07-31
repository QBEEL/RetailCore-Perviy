"""Модели базы поставщиков: карточка, структура прайса, ручная привязка.

Ключ привязки заслуживает пояснения. Строку шаблона 1С нельзя опознать ни
номером, ни артикулом: номер меняется при следующей выгрузке, а одна и та же
ячейка артикула стоит сразу в нескольких строках, различающихся только объёмом.
Зато в шаблоне есть скрытые колонки с уникальными идентификаторами
номенклатуры и характеристики — именно ими 1С опознаёт позицию у себя. На
разобранном шаблоне пара таких идентификаторов уникальна для всех строк, поэтому
она и берётся основным ключом.

Запасные ключи нужны для выгрузок без идентификаторов: артикул вместе с объёмом,
а если и объёма нет — сравнимое название.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..normalize import code_key, comparable, normalize_text
from ..pricing.mapping import SupplierProfile


@dataclass(frozen=True, slots=True)
class LinkKey:
    """Ключи строки шаблона 1С по убыванию надёжности."""

    nomenclature: str = ""
    characteristic: str = ""
    article: str = ""
    volume: str = ""
    name: str = ""

    @property
    def strong(self) -> bool:
        """Есть ли идентификаторы 1С — единственный ключ, различающий соседние строки."""
        return bool(self.nomenclature or self.characteristic)

    @property
    def identity(self) -> str:
        """Основной ключ — по нему привязка считается той же самой."""
        if self.strong:
            return f"g:{self.nomenclature}:{self.characteristic}"
        if self.article:
            return f"a:{self.article}:{self.volume}"
        return f"n:{self.name}" if self.name else ""

    def lookup(self) -> list[str]:
        """Все ключи, по которым привязку стоит попробовать найти."""
        keys: list[str] = []
        if self.strong:
            keys.append(f"g:{self.nomenclature}:{self.characteristic}")
        if self.article:
            keys.append(f"a:{self.article}:{self.volume}")
        if self.name:
            keys.append(f"n:{self.name}")
        return keys

    def __bool__(self) -> bool:
        return bool(self.identity)

    @classmethod
    def of(
        cls,
        nomenclature: object = "",
        characteristic: object = "",
        article: object = "",
        volume: object = "",
        name: object = "",
    ) -> "LinkKey":
        return cls(
            nomenclature=normalize_text(nomenclature),
            characteristic=normalize_text(characteristic),
            article=code_key(article),
            volume=normalize_text(volume),
            name=comparable(name),
        )


@dataclass(slots=True)
class SupplierLink:
    """Ручная привязка: строка 1С и товар поставщика, которые сам он не свёл."""

    key: LinkKey = field(default_factory=LinkKey)
    supplier_article: str = ""
    supplier_sku: str = ""
    supplier_ean: str = ""
    supplier_name: str = ""
    onec_article: str = ""
    onec_name: str = ""
    id: int = 0
    supplier_id: int = 0
    author: str = ""
    note: str = ""
    created_at: datetime | None = None

    @property
    def title(self) -> str:
        source = self.onec_name or self.onec_article or "строка 1С"
        target = self.supplier_name or self.supplier_article or "товар поставщика"
        return f"{source}  →  {target}"

    @property
    def target_key(self) -> str:
        return code_key(self.supplier_article)


@dataclass(slots=True)
class SupplierLayout:
    """Структура прайса: как читать файл именно этого поставщика.

    Поставщик может присылать файлы в нескольких форматах, поэтому структур у
    него несколько, а подходящая выбирается по сигнатуре заголовков и не
    затирает остальные.
    """

    profile: SupplierProfile = field(default_factory=SupplierProfile)
    signature: str = ""
    titles: list[str] = field(default_factory=list)
    id: int = 0
    supplier_id: int = 0
    uses: int = 0
    created_at: datetime | None = None
    last_used_at: datetime | None = None

    @property
    def sheet_name(self) -> str:
        return self.profile.sheet

    @property
    def summary(self) -> str:
        pairs = len(self.profile.price_map)
        sheet = f"лист «{self.profile.sheet}»" if self.profile.sheet else "лист не задан"
        return f"{sheet} · колонок цены: {pairs} · применялась {self.uses} раз"


@dataclass(slots=True)
class Supplier:
    """Карточка поставщика."""

    name: str = ""
    id: int = 0
    active: bool = True
    brands: str = ""
    categories: str = ""
    contact: str = ""
    note: str = ""
    # Отсрочка платежа в днях. Ноль — считать по истории оплат: она устойчива
    # у большинства поставщиков и точнее введённой на память.
    payment_terms_days: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Заполняются при чтении списка — нужны только для показа.
    layouts: int = 0
    links: int = 0

    @property
    def key(self) -> str:
        return normalize_text(self.name)

    @property
    def summary(self) -> str:
        parts = [f"структур: {self.layouts}", f"привязок: {self.links}"]
        if self.brands:
            parts.insert(0, self.brands)
        if self.payment_terms_days:
            parts.append(f"отсрочка {self.payment_terms_days} дн")
        return " · ".join(parts)


@dataclass(slots=True)
class Guess:
    """Догадка о том, чей это прайс, и почему так решено."""

    supplier: Supplier
    layout: SupplierLayout | None = None
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def confident(self) -> bool:
        return self.score >= 1.0

    @property
    def reason(self) -> str:
        return ", ".join(self.reasons)
