"""Доменные модели: роли полей, записи, результаты сопоставления."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FieldRole(str, Enum):
    """Семантическая роль колонки. Не зависит от конкретного файла или бренда."""

    EAN = "ean"
    ARTICLE = "article"
    SKU = "sku"
    NAME = "name"
    NAME_ALT = "name_alt"
    BRAND = "brand"
    CATEGORY = "category"
    VOLUME = "volume"
    COLOR = "color"
    SIZE = "size"
    PRICE = "price"
    QUANTITY = "quantity"
    MANUFACTURER = "manufacturer"
    DESCRIPTION = "description"
    NOTE = "note"
    DATE = "date"
    OTHER = "other"

    @property
    def title(self) -> str:
        return ROLE_TITLES.get(self, self.value)


ROLE_TITLES: dict[FieldRole, str] = {
    FieldRole.EAN: "EAN / Штрихкод",
    FieldRole.ARTICLE: "Артикул",
    FieldRole.SKU: "SKU / Код",
    FieldRole.NAME: "Наименование",
    FieldRole.NAME_ALT: "Доп. наименование",
    FieldRole.BRAND: "Бренд",
    FieldRole.CATEGORY: "Категория",
    FieldRole.VOLUME: "Объём / Фасовка",
    FieldRole.COLOR: "Цвет",
    FieldRole.SIZE: "Размер",
    FieldRole.PRICE: "Цена / РРЦ",
    FieldRole.QUANTITY: "Количество",
    FieldRole.MANUFACTURER: "Производитель",
    FieldRole.DESCRIPTION: "Описание",
    FieldRole.NOTE: "Примечание",
    FieldRole.DATE: "Дата",
    FieldRole.OTHER: "Прочее",
}

# Вес роли в общей оценке совпадения: идентификаторы важнее описаний.
DEFAULT_WEIGHTS: dict[FieldRole, float] = {
    FieldRole.ARTICLE: 1.0,
    FieldRole.SKU: 0.95,
    FieldRole.EAN: 0.95,
    FieldRole.NAME: 0.85,
    FieldRole.NAME_ALT: 0.75,
    FieldRole.BRAND: 0.6,
    FieldRole.CATEGORY: 0.5,
    FieldRole.VOLUME: 0.5,
    FieldRole.COLOR: 0.5,
    FieldRole.SIZE: 0.5,
    FieldRole.MANUFACTURER: 0.45,
    FieldRole.DESCRIPTION: 0.4,
    FieldRole.NOTE: 0.35,
    FieldRole.PRICE: 0.3,
    FieldRole.QUANTITY: 0.3,
    FieldRole.DATE: 0.3,
    FieldRole.OTHER: 0.3,
}

# Роли, по которым осмысленно искать текстом (значения по умолчанию для чекбоксов).
DEFAULT_SEARCH_ROLES: tuple[FieldRole, ...] = (
    FieldRole.ARTICLE,
    FieldRole.SKU,
    FieldRole.EAN,
    FieldRole.NAME,
    FieldRole.NAME_ALT,
    FieldRole.BRAND,
    FieldRole.CATEGORY,
    FieldRole.VOLUME,
    FieldRole.COLOR,
    FieldRole.SIZE,
    FieldRole.MANUFACTURER,
    FieldRole.DESCRIPTION,
    FieldRole.NOTE,
)


@dataclass(slots=True)
class Column:
    """Колонка таблицы с определённой (или назначенной вручную) ролью."""

    index: int
    title: str
    role: FieldRole = FieldRole.OTHER
    confidence: float = 0.0
    auto: bool = True

    @property
    def letter(self) -> str:
        n, letters = self.index + 1, ""
        while n:
            n, rem = divmod(n - 1, 26)
            letters = chr(65 + rem) + letters
        return letters


@dataclass(slots=True)
class Sheet:
    """Загруженный лист: колонки, строки и производные индексы."""

    path: str
    sheet_name: str
    header_row: int
    columns: list[Column]
    records: list["Record"]
    noise_tokens: frozenset[str] = frozenset()

    def column_for(self, role: FieldRole) -> Column | None:
        return next((c for c in self.columns if c.role == role), None)

    def columns_for(self, role: FieldRole) -> list[Column]:
        return [c for c in self.columns if c.role == role]

    @property
    def roles(self) -> set[FieldRole]:
        return {c.role for c in self.columns}


@dataclass(slots=True)
class Record:
    """Строка данных. Нормализованные представления кэшируются при загрузке."""

    row: int
    values: list[Any]
    by_role: dict[FieldRole, Any] = field(default_factory=dict)
    normalized: dict[FieldRole, str] = field(default_factory=dict)
    search_blob: str = ""
    # Название без величин и повторяющихся токенов — используется при сопоставлении.
    match_key: str = ""
    quantity: "Quantity | None" = None

    def get(self, role: FieldRole, default: Any = None) -> Any:
        value = self.by_role.get(role, default)
        return default if value is None else value

    def text(self, role: FieldRole) -> str:
        value = self.by_role.get(role)
        return "" if value is None else str(value).strip()

    @property
    def label(self) -> str:
        """Человекочитаемое имя записи для списков и карточек."""
        for role in (FieldRole.NAME, FieldRole.NAME_ALT, FieldRole.ARTICLE, FieldRole.SKU, FieldRole.EAN):
            if text := self.text(role):
                return text
        return f"Строка {self.row}"


@dataclass(frozen=True, slots=True)
class Quantity:
    """Объём/масса, приведённые к базовой единице (мл, г, шт)."""

    value: float
    unit: str

    def matches(self, other: "Quantity | None", tolerance: float = 0.05) -> bool:
        if other is None or self.unit != other.unit:
            return False
        largest = max(abs(self.value), abs(other.value))
        if largest == 0:
            return True
        return abs(self.value - other.value) / largest <= tolerance

    def __str__(self) -> str:
        value = int(self.value) if self.value.is_integer() else round(self.value, 2)
        return f"{value} {self.unit}"


class MatchStatus(str, Enum):
    MATCHED = "matched"
    REVIEW = "review"
    AMBIGUOUS = "ambiguous"
    MANUAL = "manual"
    NOT_FOUND = "not_found"

    @property
    def title(self) -> str:
        return STATUS_TITLES[self]


STATUS_TITLES: dict[MatchStatus, str] = {
    MatchStatus.MATCHED: "Совпало",
    MatchStatus.REVIEW: "Требует проверки",
    MatchStatus.AMBIGUOUS: "Неоднозначно",
    MatchStatus.MANUAL: "Вручную",
    MatchStatus.NOT_FOUND: "Не найдено",
}


@dataclass(slots=True)
class Candidate:
    """Запись-кандидат из каталога с оценкой и пояснением."""

    record: Record
    score: float
    stage: str
    # Насколько оценка снижена уточнением по объёму и названию. Нужна отдельно:
    # порог доверия проверяется по надёжности этапа, а не по результату
    # уточнения, задача которого — лишь развести кандидатов между собой.
    penalty: float = 0.0
    volume_conflict: bool = False
    # Из какого каталога взята запись: имя файла для показа и номер для приоритета.
    origin: str = ""
    catalog: int = 0


@dataclass(slots=True)
class MatchResult:
    """Результат сопоставления одной строки цели с каталогом."""

    target: Record
    candidate: Candidate | None = None
    status: MatchStatus = MatchStatus.NOT_FOUND
    alternatives: list[Candidate] = field(default_factory=list)

    @property
    def source(self) -> Record | None:
        return self.candidate.record if self.candidate else None

    @property
    def score(self) -> float:
        return self.candidate.score if self.candidate else 0.0

    @property
    def stage_score(self) -> float:
        """Оценка этапа без уточняющего штрафа — по ней проверяется порог."""
        return self.candidate.score + self.candidate.penalty if self.candidate else 0.0

    @property
    def stage(self) -> str:
        return self.candidate.stage if self.candidate else ""

    @property
    def origin(self) -> str:
        return self.candidate.origin if self.candidate else ""

    def assign(self, record: Record, stage: str = "Вручную", origin: str = "") -> None:
        self.candidate = Candidate(record, 100.0, stage, origin=origin)
        self.status = MatchStatus.MANUAL

    def clear(self) -> None:
        self.candidate = None
        self.status = MatchStatus.NOT_FOUND
