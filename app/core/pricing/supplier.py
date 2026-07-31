"""Чтение прайс-листа поставщика.

Структура заранее не известна и у каждого поставщика своя, поэтому роли колонок
определяются автоматически тем же механизмом, что и на остальных страницах, а
пользователь может поправить выбор вручную — исправления хранятся в профиле
поставщика и применяются к следующим файлам.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..models import Column, FieldRole, Record, Sheet
from ..normalize import detect_noise_tokens, extract_quantity, normalize_text, parse_quantity
from ..schema import clean_value, detect_columns, detect_header_row
from ..workbook import prepare_record, read_raw

# Доля заполненных числовых значений, с которой колонка считается ценовой.
_PRICE_FILL_RATIO = 0.25
# Штрихкод — тоже число, но ценой быть не может.
_NOT_PRICE_ROLES = frozenset({FieldRole.EAN, FieldRole.DATE})
_PRICE_WORDS = ("цена", "ррц", "прайс", "закуп", "стоимость", "price", "rrc", "rrp", "msrp")
_PERCENT_WORDS = ("%", "процент", "увеличен", "наценк", "скидк", "percent")


@dataclass(slots=True)
class SupplierColumn:
    """Колонка прайса, пригодная как источник цены."""

    index: int
    title: str
    filled: int
    priced: bool = False

    @property
    def label(self) -> str:
        return f"{self.title} ({self.filled} знач.)" if self.filled else self.title


@dataclass(slots=True)
class SupplierPrice:
    """Разобранный прайс поставщика."""

    path: str
    sheet_name: str
    header_row: int
    columns: list[Column]
    records: list[Record]
    price_columns: list[SupplierColumn] = field(default_factory=list)
    noise_tokens: frozenset[str] = frozenset()

    @property
    def titles(self) -> list[str]:
        return [c.title for c in self.columns]

    def as_sheet(self) -> Sheet:
        return Sheet(
            path=self.path,
            sheet_name=self.sheet_name,
            header_row=self.header_row,
            columns=self.columns,
            records=self.records,
            noise_tokens=self.noise_tokens,
        )

    def column_by_title(self, title: str) -> SupplierColumn | None:
        if not title:
            return None
        wanted = normalize_text(title)
        return next((c for c in self.price_columns if normalize_text(c.title) == wanted), None)

    def price_of(self, record: Record, column: SupplierColumn | None) -> float | None:
        """Значение ценовой колонки строки — как число, без текста и формул."""
        if column is None or column.index >= len(record.values):
            return None
        return as_price(record.values[column.index])


def load_supplier(
    path: str,
    sheet_name: str | None = None,
    role_overrides: dict[int, FieldRole] | None = None,
    progress=None,
) -> SupplierPrice:
    """Читает прайс: роли колонок, строки товаров и список ценовых колонок."""
    rows, resolved = read_raw(path, sheet_name)
    if not rows:
        raise ValueError("Прайс поставщика не содержит данных")

    header = detect_header_row(rows)
    head = rows[header] if header < len(rows) else []
    body = [row for row in rows[header + 1:] if any(v is not None for v in row)]
    if not body:
        raise ValueError("В прайсе поставщика нет строк с данными")

    columns = detect_columns(head, body)
    for index, role in (role_overrides or {}).items():
        if 0 <= index < len(columns):
            columns[index].role = role
            columns[index].auto = False

    name_columns = [c.index for c in columns if c.role in (FieldRole.NAME, FieldRole.NAME_ALT)]
    noise = detect_noise_tokens(
        " ".join(str(row[i]) for i in name_columns if i < len(row) and row[i] is not None)
        for row in body
    )

    records = _records(body, columns, noise, header, progress)
    if not records:
        raise ValueError(
            "В прайсе поставщика не найдено ни одной товарной строки — "
            "проверьте, что выбран нужный лист")
    return SupplierPrice(
        path=path,
        sheet_name=resolved,
        header_row=header,
        columns=columns,
        records=records,
        price_columns=_price_columns(columns, body),
        noise_tokens=noise,
    )


def _records(
    body: Sequence[Sequence[Any]],
    columns: Sequence[Column],
    noise: frozenset[str],
    header: int,
    progress=None,
) -> list[Record]:
    """Строки-разделители категорий пропускаются: у них нет ни артикула, ни кода."""
    roles = [(c.index, c.role) for c in columns if c.role is not FieldRole.OTHER]
    identifiers = (FieldRole.ARTICLE, FieldRole.SKU, FieldRole.EAN)
    records: list[Record] = []
    total = len(body)
    for offset, row in enumerate(body):
        values = list(row)
        by_role = {
            role: values[index]
            for index, role in roles
            if index < len(values) and values[index] is not None
        }
        if not any(by_role.get(role) for role in identifiers):
            continue
        record = Record(row=header + offset + 2, values=values, by_role=by_role)
        prepare_record(record, noise)
        _ensure_quantity(record)
        records.append(record)
        if progress and (offset % 250 == 0 or offset == total - 1):
            progress(offset + 1, total)
    return records


def _ensure_quantity(record: Record) -> None:
    """Объём часто указан не в названии, а в категории: «Духи ... 10 мл».

    Без него не различить варианты одной номенклатуры, поэтому категория и
    доп. наименование проверяются как запасные источники.
    """
    if record.quantity is not None:
        return
    for role in (FieldRole.CATEGORY, FieldRole.NAME_ALT, FieldRole.SIZE):
        value = record.by_role.get(role)
        if quantity := (extract_quantity(value) or parse_quantity(value)):
            record.quantity = quantity
            return


def _price_columns(columns: Sequence[Column], body: Sequence[Sequence[Any]]) -> list[SupplierColumn]:
    """Колонки, которые могут содержать цену: числовые и не служебные."""
    total = len(body) or 1
    found: list[SupplierColumn] = []
    for column in columns:
        if column.role in _NOT_PRICE_ROLES:
            continue
        filled = sum(
            1 for row in body
            if column.index < len(row) and as_price(row[column.index]) is not None
        )
        title = normalize_text(column.title)
        if any(word in title for word in _PERCENT_WORDS):
            continue
        if filled / total < _PRICE_FILL_RATIO:
            continue
        found.append(SupplierColumn(
            index=column.index,
            title=column.title,
            filled=filled,
            priced=any(word in title for word in _PRICE_WORDS),
        ))
    return found


def as_price(value: Any) -> float | None:
    """Число из ячейки. Текст, формулы, ноль и отрицательные ценой не считаются."""
    value = clean_value(value)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
        try:
            number = float(text)
        except ValueError:
            return None
    return number if number > 0 else None
