"""Отчётность по акциям: роли колонок, метрики, правила магазинов, профили.

Модель строится вокруг одной нормализованной строки продаж (`SaleRow`). Откуда
она пришла — из сводной по всем магазинам или из файла одного менеджера по
одному магазину — дальше по конвейеру неважно: правила объединения магазинов,
сводная и оформление работают с одним и тем же типом.

Профиль отчёта описывает, что показать поставщику: какие поля идут в строки,
какие в колонки, какие метрики считаются и что отсекается фильтром. Он один на
поставщика и живёт в общей базе — иначе у каждого менеджера получился бы свой
формат, а сверять их пришлось бы руками.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Iterable, Sequence

MONTHS: tuple[str, ...] = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)


class Field(str, Enum):
    """Роль колонки в исходной выгрузке.

    Значения совпадают с именами полей `SaleRow` — так разбор, фильтры и
    сводная обходятся без таблицы соответствий между ролью и атрибутом.
    """

    STORE = "store"
    ITEM = "item"
    CATEGORY = "category"
    BRAND = "brand"
    PROMO = "promo"
    PERCENT = "percent"
    YEAR = "year"
    MONTH = "month"
    QTY = "qty"
    AUTO_DISCOUNT = "auto_discount"
    BONUS_DISCOUNT = "bonus_discount"
    REVENUE = "revenue"
    BONUS_ACCRUED = "bonus_accrued"

    @property
    def title(self) -> str:
        return FIELD_TITLES[self]

    @property
    def numeric(self) -> bool:
        """Поле-число, которое можно суммировать в метрике."""
        return self in NUMERIC_FIELDS


FIELD_TITLES: dict[Field, str] = {
    Field.STORE: "Магазин",
    Field.ITEM: "Номенклатура",
    Field.CATEGORY: "Категория товара",
    Field.BRAND: "Бренд",
    Field.PROMO: "Акция",
    Field.PERCENT: "% скидки",
    Field.YEAR: "Год",
    Field.MONTH: "Месяц",
    Field.QTY: "Количество",
    Field.AUTO_DISCOUNT: "Сумма авт.",
    Field.BONUS_DISCOUNT: "Скидка бонусами",
    Field.REVENUE: "Сумма",
    Field.BONUS_ACCRUED: "Начислено бонусов",
}

NUMERIC_FIELDS: frozenset[Field] = frozenset({
    Field.QTY, Field.AUTO_DISCOUNT, Field.BONUS_DISCOUNT,
    Field.REVENUE, Field.BONUS_ACCRUED,
})

# Поля, пригодные на роль строк и колонок сводной: числа группировать незачем.
GROUP_FIELDS: tuple[Field, ...] = (
    Field.ITEM, Field.PROMO, Field.STORE, Field.BRAND,
    Field.CATEGORY, Field.PERCENT, Field.MONTH, Field.YEAR,
)


@dataclass(slots=True)
class SaleRow:
    """Одна строка продаж после разбора исходника.

    `source_store` хранит магазин до применения правил объединения. Без него
    невозможно объяснить пользователю, почему в отчёте у «Универмага» цифры
    больше, чем в исходном файле.
    """

    store: str = ""
    item: str = ""
    category: str = ""
    brand: str = ""
    promo: str = ""
    percent: str = ""
    year: int = 0
    month: int = 0
    qty: float = 0.0
    auto_discount: float = 0.0
    bonus_discount: float = 0.0
    revenue: float = 0.0
    bonus_accrued: float = 0.0
    source_store: str = ""
    source_file: str = ""

    def value(self, role: Field) -> Any:
        return getattr(self, role.value)

    def text(self, role: Field) -> str:
        """Значение поля как подпись строки или колонки сводной."""
        raw = self.value(role)
        if role is Field.MONTH:
            return MONTHS[int(raw) - 1] if 1 <= int(raw or 0) <= 12 else ""
        if role is Field.YEAR:
            return str(int(raw)) if raw else ""
        return str(raw or "")

    @property
    def total_discount(self) -> float:
        return self.auto_discount + self.bonus_discount


class Metric(str, Enum):
    """Показатель в отчёте. Отдельно от `Field`: у метрики своя подпись,
    свой формат и своё правило расчёта — «скидка всего» не колонка исходника,
    а сумма двух."""

    QTY = "qty"
    AUTO_DISCOUNT = "auto_discount"
    BONUS_DISCOUNT = "bonus_discount"
    TOTAL_DISCOUNT = "total_discount"
    REVENUE = "revenue"
    BONUS_ACCRUED = "bonus_accrued"

    @property
    def title(self) -> str:
        return METRIC_TITLES[self]

    @property
    def money(self) -> bool:
        return self is not Metric.QTY

    def of(self, row: SaleRow) -> float:
        if self is Metric.TOTAL_DISCOUNT:
            return row.total_discount
        return float(getattr(row, self.value) or 0.0)


METRIC_TITLES: dict[Metric, str] = {
    Metric.QTY: "Количество, шт",
    Metric.AUTO_DISCOUNT: "Сумма скидки, руб",
    Metric.BONUS_DISCOUNT: "Скидка бонусами, руб",
    Metric.TOTAL_DISCOUNT: "Скидка всего, руб",
    Metric.REVENUE: "Сумма продаж, руб",
    Metric.BONUS_ACCRUED: "Начислено бонусов, руб",
}


@dataclass(slots=True)
class StoreRule:
    """Правило «магазин-источник → магазин-приёмник».

    Продажи источника переносятся приёмнику. Правила общие для всех менеджеров
    и применяются транзитивно: если A→B и B→C, то продажи A уедут в C.
    """

    id: int = 0
    source: str = ""
    target: str = ""
    enabled: bool = True
    comment: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""

    @property
    def valid(self) -> bool:
        return bool(self.source.strip() and self.target.strip())

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "target": self.target,
                "enabled": self.enabled, "comment": self.comment}


@dataclass(slots=True)
class ReportFilter:
    """Что отсекается перед построением сводной.

    `promo_only` — то самое «удаление лишних данных», которое сейчас делается
    руками: строки без акции в отчёт поставщику не идут.
    """

    promo_only: bool = True
    brands: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    stores: list[str] = field(default_factory=list)
    promos: list[str] = field(default_factory=list)

    def allows(self, row: SaleRow) -> bool:
        if self.promo_only and not row.promo.strip():
            return False
        if self.brands and row.brand not in self.brands:
            return False
        if self.categories and row.category not in self.categories:
            return False
        if self.stores and row.store not in self.stores:
            return False
        if self.promos and row.promo not in self.promos:
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {"promo_only": self.promo_only, "brands": self.brands,
                "categories": self.categories, "stores": self.stores,
                "promos": self.promos}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportFilter":
        return cls(
            promo_only=bool(data.get("promo_only", True)),
            brands=_strings(data.get("brands")),
            categories=_strings(data.get("categories")),
            stores=_strings(data.get("stores")),
            promos=_strings(data.get("promos")),
        )


@dataclass(slots=True)
class ReportProfile:
    """Формат отчёта для одного поставщика.

    Значения по умолчанию повторяют отчёт «Июнь 2026», собранный руками: строки
    — номенклатура, колонки — акция, метрики — количество и сумма авт. скидки.
    Новому менеджеру достаточно завести профиль со своим поставщиком, ничего не
    меняя в коде.
    """

    id: int = 0
    name: str = ""
    supplier: str = ""
    supplier_id: int = 0
    rows: list[Field] = field(default_factory=lambda: [Field.ITEM])
    columns: list[Field] = field(default_factory=lambda: [Field.PROMO])
    metrics: list[Metric] = field(
        default_factory=lambda: [Metric.QTY, Metric.AUTO_DISCOUNT])
    filters: ReportFilter = field(default_factory=ReportFilter)
    # Шапка и подписи. Пустой заголовок означает «собрать из имени поставщика
    # и периода» — руками его заполняют редко.
    title: str = ""
    note: str = ""
    signatures: list[str] = field(default_factory=list)
    file_name: str = "{Месяц} {Год}"
    stores_sheet: bool = False
    apply_store_rules: bool = True
    updated_at: datetime | None = None
    updated_by: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.supplier or "Без названия"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "supplier": self.supplier,
            "supplier_id": self.supplier_id,
            "rows": [role.value for role in as_fields(self.rows)],
            "columns": [role.value for role in as_fields(self.columns)],
            "metrics": [metric.value for metric in as_metrics(self.metrics)],
            "filters": self.filters.as_dict(),
            "title": self.title,
            "note": self.note,
            "signatures": self.signatures,
            "file_name": self.file_name,
            "stores_sheet": self.stores_sheet,
            "apply_store_rules": self.apply_store_rules,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportProfile":
        profile = cls(
            id=int(data.get("id") or 0),
            name=str(data.get("name") or ""),
            supplier=str(data.get("supplier") or ""),
            supplier_id=int(data.get("supplier_id") or 0),
            title=str(data.get("title") or ""),
            note=str(data.get("note") or ""),
            signatures=_strings(data.get("signatures")),
            file_name=str(data.get("file_name") or "{Месяц} {Год}"),
            stores_sheet=bool(data.get("stores_sheet", False)),
            apply_store_rules=bool(data.get("apply_store_rules", True)),
            filters=ReportFilter.from_dict(data.get("filters") or {}),
        )
        if fields := _fields(data.get("rows")):
            profile.rows = fields
        if fields := _fields(data.get("columns")):
            profile.columns = fields
        if metrics := _metrics(data.get("metrics")):
            profile.metrics = metrics
        return profile


@dataclass(slots=True)
class Period:
    """Период отчёта. Берётся из колонок «Год» и «Месяц» самих данных."""

    year: int = 0
    month: int = 0

    @property
    def known(self) -> bool:
        return bool(self.year and 1 <= self.month <= 12)

    @property
    def month_name(self) -> str:
        return MONTHS[self.month - 1] if 1 <= self.month <= 12 else ""

    @property
    def title(self) -> str:
        return f"{self.month_name} {self.year}".strip() if self.known else ""

    def range(self) -> tuple[date, date] | None:
        """Первый и последний день месяца — для строки «Период:» в шапке."""
        if not self.known:
            return None
        first = date(self.year, self.month, 1)
        if self.month == 12:
            last = date(self.year, 12, 31)
        else:
            last = date(self.year, self.month + 1, 1).toordinal() - 1
            last = date.fromordinal(last)
        return first, last

    def __str__(self) -> str:
        return self.title


@dataclass(slots=True)
class Cell:
    """Значение одной метрики на пересечении строки и колонки."""

    metric: Metric
    value: float = 0.0

    @property
    def empty(self) -> bool:
        return not self.value


@dataclass(slots=True)
class ReportRow:
    """Строка готовой сводной."""

    keys: tuple[str, ...] = ()
    cells: list[Cell] = field(default_factory=list)

    @property
    def label(self) -> str:
        return " · ".join(k for k in self.keys if k)


@dataclass(slots=True)
class ColumnGroup:
    """Группа колонок — одно значение разреза со всеми метриками под ним."""

    keys: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return " · ".join(k for k in self.keys if k) or "Всего"


@dataclass(slots=True)
class ReportTable:
    """Готовая сводная: заголовки, строки, итог и всё для шапки файла."""

    profile: ReportProfile
    period: Period = field(default_factory=Period)
    row_fields: list[Field] = field(default_factory=list)
    column_fields: list[Field] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    groups: list[ColumnGroup] = field(default_factory=list)
    rows: list[ReportRow] = field(default_factory=list)
    totals: list[Cell] = field(default_factory=list)
    # Что было отброшено и что переехало — показывается на вкладке до выгрузки,
    # чтобы «куда делись строки» не выяснялось уже у поставщика.
    source_rows: int = 0
    used_rows: int = 0
    dropped_rows: int = 0
    moved_rows: int = 0
    stores: list[str] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    store_totals: list[tuple[str, list[Cell]]] = field(default_factory=list)

    @property
    def column_count(self) -> int:
        return len(self.groups) * len(self.metrics)

    @property
    def empty(self) -> bool:
        return not self.rows

    def file_name(self) -> str:
        """Имя файла по шаблону профиля: «Июнь 2026»."""
        template = self.profile.file_name or "{Месяц} {Год}"
        name = (template
                .replace("{Месяц}", self.period.month_name)
                .replace("{Год}", str(self.period.year) if self.period.year else "")
                .replace("{Поставщик}", self.profile.supplier)
                .replace("{Профиль}", self.profile.display_name))
        name = " ".join(name.split())
        return name or self.profile.display_name

    def header_title(self) -> str:
        if self.profile.title:
            return (self.profile.title
                    .replace("{Месяц}", self.period.month_name)
                    .replace("{Год}", str(self.period.year) if self.period.year else "")
                    .replace("{Поставщик}", self.profile.supplier))
        supplier = self.profile.supplier or self.profile.display_name
        return f"Отчёт по акциям · {supplier}".strip(" ·")


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if str(item).strip()]


def _fields(value: Any) -> list[Field]:
    return [f for name in _strings(value) if (f := parse_field(name))]


def _metrics(value: Any) -> list[Metric]:
    return [m for name in _strings(value) if (m := parse_metric(name))]


def parse_field(value: Any) -> Field | None:
    if isinstance(value, Field):
        return value
    try:
        return Field(str(value))
    except ValueError:
        return None


def parse_metric(value: Any) -> Metric | None:
    if isinstance(value, Metric):
        return value
    try:
        return Metric(str(value))
    except ValueError:
        return None


def as_fields(values: Any) -> list[Field]:
    """Приводит список к ролям, чем бы он ни был заполнен.

    `Field` наследует `str`, и это делает его уязвимым на любой границе, где
    объект проходит через чужой контейнер: Qt, например, хранит такое значение
    как строку и возвращает уже `str`, а не член перечисления. Внешне список
    выглядит прежним — сравнение с ролью проходит, подпись находится, — и
    ломается лишь там, где нужен сам объект (`role.value`). Приведение здесь
    закрывает вопрос независимо от того, кто заполнил список.
    """
    if not isinstance(values, (list, tuple)):
        return []
    return [role for value in values if (role := parse_field(value))]


def as_metrics(values: Any) -> list[Metric]:
    """То же для показателей — по той же причине."""
    if not isinstance(values, (list, tuple)):
        return []
    return [metric for value in values if (metric := parse_metric(value))]


def period_of(rows: Iterable[SaleRow]) -> Period:
    """Период данных: самый частый месяц среди строк.

    Не минимум и не максимум: в выгрузку иногда попадает одна-две строки
    соседнего месяца (возврат, поздняя проводка), и по ним нельзя называть
    файл. Побеждает месяц, которым помечено большинство строк.
    """
    counts: dict[tuple[int, int], int] = {}
    for row in rows:
        if row.year and row.month:
            key = (int(row.year), int(row.month))
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return Period()
    year, month = max(counts, key=lambda key: (counts[key], key))
    return Period(year=year, month=month)


def default_profile(supplier: str = "") -> ReportProfile:
    """Профиль, повторяющий отчёт, который собирался руками."""
    return ReportProfile(name=supplier or "Отчёт по акциям", supplier=supplier)


def metric_titles(metrics: Sequence[Metric]) -> list[str]:
    return [m.title for m in metrics]
