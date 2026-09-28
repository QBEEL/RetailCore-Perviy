"""Строки чеков и выгрузка целиком — то, во что превращается файл 1С."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# Покупатель без карты. Гостем его не считаем: одна строка на сотни разных
# людей дала бы «самого частого гостя», которого не существует.
ANONYMOUS = "розничный покупатель"

# Продавец не выбран: покупатель взял товар сам. Отдельно, а не продавцом —
# иначе он всегда был бы «лучшим продавцом» магазина.
SELF_SERVICE = "самостоятельная покупка"


@dataclass(slots=True)
class Line:
    """Товар в чеке. Суммы у возврата отрицательные — так их пишет 1С."""

    document: str = ""
    kind: str = ""
    day: date | None = None
    moment: datetime | None = None
    store: str = ""
    guest: str = ""
    item: str = ""
    category: str = ""
    brand: str = ""
    price: float = 0.0
    promo: str = ""
    auto_percent: float = 0.0
    seller: str = ""
    quantity: float = 0.0
    cost: float = 0.0
    auto_discount: float = 0.0
    bonus_discount: float = 0.0
    amount: float = 0.0
    bonus_accrued: float = 0.0
    source: str = ""

    @property
    def is_return(self) -> bool:
        return "возврат" in self.kind.lower() or self.quantity < 0 or self.amount < 0

    @property
    def discount(self) -> float:
        """Скидка строки: автоматическая и списанные бонусы вместе."""
        return self.auto_discount + self.bonus_discount

    @property
    def profit(self) -> float:
        """Валовая прибыль: выручка без себестоимости."""
        return self.amount - self.cost

    @property
    def known_guest(self) -> bool:
        return bool(self.guest) and self.guest.strip().lower() != ANONYMOUS

    @property
    def self_service(self) -> bool:
        return not self.seller or self.seller.strip().lower() == SELF_SERVICE


@dataclass(slots=True)
class Receipts:
    """Разобранная выгрузка «Аналитика по чекам»."""

    lines: list[Line] = field(default_factory=list)
    start: date | None = None
    end: date | None = None
    # Строки «Подразделение» и «Отбор» из параметров отчёта — как их напечатала
    # 1С, обрезанными: полный список подразделений в шапке не помещается.
    units: str = ""
    selection: str = ""
    sources: list[str] = field(default_factory=list)
    # Колонки, которых в выгрузке нет. Отчёт в 1С настраивается, и выводы по
    # отсутствующей колонке не показываются, а не считаются нулём.
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Сошлись ли строки со строкой «Итого» самого файла. None — итога нет.
    balanced: bool | None = None

    @property
    def period_title(self) -> str:
        if self.start and self.end:
            return f"{self.start:%d.%m.%Y} — {self.end:%d.%m.%Y}"
        return "период не указан"

    @property
    def days(self) -> int:
        if self.start and self.end and self.end >= self.start:
            return (self.end - self.start).days + 1
        return 0

    def has(self, column: str) -> bool:
        return column not in self.missing
