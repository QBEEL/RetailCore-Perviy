"""Доменные модели переоценки: виды цен, строки шаблона, итоги сравнения."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..models import Candidate, FieldRole, Quantity, Record

# Цены сравниваются с точностью до копейки: 1345.0 и 1345.004 — одна и та же цена.
PRICE_EPSILON = 0.005


class PriceStatus(str, Enum):
    """Судьба строки шаблона 1С после сопоставления и сравнения цен."""

    CHANGED = "changed"
    UNCHANGED = "unchanged"
    REVIEW = "review"
    NO_PRICE = "no_price"
    NOT_FOUND = "not_found"

    @property
    def title(self) -> str:
        return PRICE_STATUS_TITLES[self]


PRICE_STATUS_TITLES: dict[PriceStatus, str] = {
    PriceStatus.CHANGED: "Цена изменена",
    PriceStatus.UNCHANGED: "Без изменений",
    PriceStatus.REVIEW: "Требует сопоставления",
    PriceStatus.NO_PRICE: "Нет цены у поставщика",
    PriceStatus.NOT_FOUND: "Не найдено",
}

# Цвет статуса берётся страницей из общей палитры по этому ключу.
PRICE_STATUS_TONES: dict[PriceStatus, str] = {
    PriceStatus.CHANGED: "matched",
    PriceStatus.UNCHANGED: "ambiguous",
    PriceStatus.REVIEW: "review",
    PriceStatus.NO_PRICE: "review",
    PriceStatus.NOT_FOUND: "not_found",
}


@dataclass(slots=True)
class PriceType:
    """Вид цены шаблона 1С и номера его колонок (1-based, как в Excel).

    Номера берутся со служебного листа шаблона, где 1С их прямо перечисляет,
    поэтому гадать по объединённой шапке не приходится.
    """

    name: str
    guid: str = ""
    old_column: int = 0
    percent_column: int = 0
    price_column: int = 0
    unit_column: int = 0
    unit_guid_column: int = 0

    @property
    def valid(self) -> bool:
        return bool(self.name) and self.price_column > 0


@dataclass(slots=True)
class PriceCell:
    """Цена одного вида для одной строки: что было и что станет."""

    type_index: int
    old: float | None = None
    new: float | None = None

    @property
    def known(self) -> bool:
        return self.new is not None

    @property
    def changed(self) -> bool:
        if self.new is None:
            return False
        if self.old is None:
            return True
        return abs(self.new - self.old) > PRICE_EPSILON

    @property
    def delta(self) -> float | None:
        if self.new is None or self.old is None:
            return None
        return self.new - self.old

    @property
    def percent(self) -> float | None:
        delta = self.delta
        if delta is None or not self.old:
            return None
        return delta / self.old * 100.0


@dataclass(slots=True)
class PriceLine:
    """Строка шаблона 1С и всё, что о ней известно после сопоставления."""

    record: Record
    row: int
    article: str
    name: str
    quantity: Quantity | None = None
    status: PriceStatus = PriceStatus.NOT_FOUND
    candidate: Candidate | None = None
    alternatives: list[Candidate] = field(default_factory=list)
    cells: list[PriceCell] = field(default_factory=list)
    manual: bool = False
    # Вариант подставлен сохранённой привязкой из базы поставщиков.
    linked: bool = False
    # У привязки разошлись названия: за артикулом мог оказаться другой товар.
    link_warning: bool = False

    @property
    def matched(self) -> bool:
        return self.candidate is not None

    @property
    def source(self) -> Record | None:
        return self.candidate.record if self.candidate else None

    @property
    def score(self) -> float:
        return self.candidate.score if self.candidate else 0.0

    @property
    def method(self) -> str:
        if self.manual:
            return "Вручную"
        return self.candidate.stage if self.candidate else ""

    @property
    def supplier_article(self) -> str:
        source = self.source
        return source.text(FieldRole.ARTICLE) if source else ""

    @property
    def writable(self) -> bool:
        """Есть ли что записывать в шаблон: хотя бы одна изменившаяся цена."""
        return self.status is PriceStatus.CHANGED and any(c.changed for c in self.cells)

    def cell(self, type_index: int) -> PriceCell | None:
        return next((c for c in self.cells if c.type_index == type_index), None)

    def assign(self, candidate: Candidate, *, manual: bool = True) -> None:
        """Выбор варианта. Прежние альтернативы остаются доступны.

        `manual=False` — вариант подставлен сохранённой привязкой: в этом
        прогоне пользователь ничего не выбирал, и способ должен показывать
        «Привязка», а не «Вручную».
        """
        if self.candidate is not None and self.candidate is not candidate:
            others = [self.candidate, *self.alternatives]
            self.alternatives = [c for c in others if c is not candidate]
        else:
            self.alternatives = [c for c in self.alternatives if c is not candidate]
        self.candidate = candidate
        self.manual = manual

    def clear(self) -> None:
        if self.candidate is not None:
            self.alternatives.insert(0, self.candidate)
        self.candidate = None
        self.manual = False
        self.linked = False
        self.link_warning = False
        self.cells = []
        self.status = PriceStatus.NOT_FOUND


@dataclass(slots=True)
class PriceStats:
    """Итоги обработки — то, что показывается плитками после сравнения."""

    total: int = 0
    found: int = 0
    not_found: int = 0
    changed: int = 0
    unchanged: int = 0
    review: int = 0
    no_price: int = 0

    @property
    def rate(self) -> float:
        """Процент совпадения: сколько строк шаблона нашли себя у поставщика."""
        return self.found / self.total * 100.0 if self.total else 0.0

    @classmethod
    def of(cls, lines: "list[PriceLine]") -> "PriceStats":
        stats = cls(total=len(lines))
        for line in lines:
            if line.matched:
                stats.found += 1
            if line.status is PriceStatus.CHANGED:
                stats.changed += 1
            elif line.status is PriceStatus.UNCHANGED:
                stats.unchanged += 1
            elif line.status is PriceStatus.REVIEW:
                stats.review += 1
            elif line.status is PriceStatus.NO_PRICE:
                stats.no_price += 1
            else:
                stats.not_found += 1
        return stats
