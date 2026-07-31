"""Чтение шаблона выгрузки цен из 1С.

Шапка шаблона двухуровневая: в верхней строке — название вида цены, в нижней
подписи его колонок («Старая цена», «Изменение», «%», «Цена», «Ед. изм.»).
Автоопределение ролей здесь бесполезно — подписи повторяются столько раз,
сколько в базе видов цен.

Зато 1С кладёт в книгу служебный лист (имя — один пробел), где прямо перечисляет
номера колонок каждого вида цены. Он и служит основным источником структуры;
разбор шапки остаётся запасным путём для выгрузок без такого листа.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..models import Column, FieldRole, Record, Sheet
from ..normalize import detect_noise_tokens, normalize_text
from ..workbook import list_sheets, prepare_record, read_raw
from .models import PriceType

# Подписи служебного листа. Порядок проверки важен: «Старая цена» содержит
# «цена», поэтому длинные варианты идут первыми.
_SERVICE_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("old_column", ("старая цена", "прежняя цена")),
    ("percent_column", ("процент изменения", "процент", "изменение")),
    ("unit_column", ("единица измерения", "ед изм")),
    ("unit_guid_column", ("уникальный идентификатор", "идентификатор")),
    ("price_column", ("цена",)),
)
_SERVICE_MARK = "номер колонки"
_TYPE_NAME_WORDS = ("вид цены", "тип цены")
_GUID_WORDS = ("уникальный идентификатор", "идентификатор")

# Подписи нижней строки шапки — запасной разбор, если служебного листа нет.
_HEADER_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("old_column", ("старая цена", "прежняя цена")),
    ("percent_column", ("процент", "изменение", "%")),
    ("unit_column", ("ед изм", "единица")),
    ("unit_guid_column", ("уникальный идентификатор", "идентификатор")),
    ("price_column", ("цена", "новая цена")),
)

_ARTICLE_WORDS = ("артикул", "article")
_NAME_WORDS = ("товар", "номенклатура", "наименование", "название")
_SKU_WORDS = ("код",)
_EAN_WORDS = ("штрихкод", "штрих код", "ean", "barcode")
_HEADER_SCAN_ROWS = 12

# Скрытые колонки шаблона с идентификаторами 1С. Их пара уникально опознаёт
# строку и переживает и переименование товара, и новую выгрузку шаблона,
# поэтому именно ею ключуются сохранённые привязки.
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_NOMENCLATURE_WORDS = ("номенклатур",)
_CHARACTERISTIC_WORDS = ("характеристик", "вариант", "модификац")


@dataclass(slots=True)
class OneCTemplate:
    """Разобранный шаблон 1С: строки, ключевые колонки и виды цен."""

    path: str
    sheet_name: str
    header_row: int
    titles: list[str]
    records: list[Record]
    price_types: list[PriceType] = field(default_factory=list)
    article_column: int = 0
    name_column: int = 0
    sku_column: int = 0
    ean_column: int = 0
    nomenclature_column: int = 0
    characteristic_column: int = 0
    noise_tokens: frozenset[str] = frozenset()

    @property
    def has_identifiers(self) -> bool:
        """Есть ли в шаблоне идентификаторы 1С — самый надёжный ключ строки."""
        return bool(self.nomenclature_column or self.characteristic_column)

    def value_at(self, record: Record, column: int) -> str:
        """Значение колонки строки по её номеру (1-based), как в Excel."""
        index = column - 1
        if index < 0 or index >= len(record.values):
            return ""
        return _text(record.values[index])

    def as_sheet(self) -> Sheet:
        """Представление для движка сопоставления."""
        columns = [
            Column(index=index, title=title or f"Колонка {index + 1}", role=self._role(index + 1))
            for index, title in enumerate(self.titles)
        ]
        return Sheet(
            path=self.path,
            sheet_name=self.sheet_name,
            header_row=self.header_row - 1,
            columns=columns,
            records=self.records,
            noise_tokens=self.noise_tokens,
        )

    def _role(self, column: int) -> FieldRole:
        for number, role in (
            (self.article_column, FieldRole.ARTICLE),
            (self.name_column, FieldRole.NAME),
            (self.sku_column, FieldRole.SKU),
            (self.ean_column, FieldRole.EAN),
        ):
            if number and number == column:
                return role
        return FieldRole.OTHER

    @property
    def valid_types(self) -> list[PriceType]:
        return [t for t in self.price_types if t.valid]


def load_template(path: str, sheet_name: str | None = None, progress=None) -> OneCTemplate:
    """Читает шаблон 1С целиком: строки товаров и описание видов цен."""
    rows, resolved = read_raw(path, sheet_name)
    if not rows:
        raise ValueError("Шаблон 1С не содержит данных")

    header = _header_row(rows)
    titles = _titles(rows, header)
    columns = _key_columns(titles, rows, header)
    if not columns["article"] and not columns["name"]:
        raise ValueError(
            "В шаблоне 1С не найдены колонки «Артикул» и «Товар» — "
            "проверьте, что выбран лист с товарами")

    types = _service_types(path, sheet_name or resolved) or _header_types(rows, header, titles)
    if not types:
        raise ValueError(
            "В шаблоне 1С не найдено ни одного вида цены "
            "(колонки «Старая цена» и «Цена»)")

    records = _records(rows, header, columns, progress)
    nomenclature, characteristic = _guid_columns(titles, rows, header, types, columns)
    template = OneCTemplate(
        path=path,
        sheet_name=resolved,
        header_row=header + 1,
        titles=titles,
        records=records,
        price_types=types,
        article_column=columns["article"],
        name_column=columns["name"],
        sku_column=columns["sku"],
        ean_column=columns["ean"],
        nomenclature_column=nomenclature,
        characteristic_column=characteristic,
    )
    template.noise_tokens = detect_noise_tokens(
        r.by_role.get(FieldRole.NAME) for r in records)
    for record in records:
        prepare_record(record, template.noise_tokens)
    return template


# --- структура ----------------------------------------------------------------

def _header_row(rows: Sequence[Sequence[Any]]) -> int:
    """Нижняя строка шапки — та, где больше всего коротких подписей."""
    best, best_score = 0, -1
    for index, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        labels = sum(1 for v in row if isinstance(v, str) and 0 < len(v.strip()) <= 60)
        if labels > best_score:
            best, best_score = index, labels
    return best


def _titles(rows: Sequence[Sequence[Any]], header: int) -> list[str]:
    """Подпись колонки — своя плюс группа строкой выше: «Закупочная · Цена»."""
    width = max((len(r) for r in rows[: header + 2]), default=0)
    above = rows[header - 1] if header > 0 else []
    current = rows[header] if header < len(rows) else []
    titles: list[str] = []
    group = ""
    for column in range(width):
        # Объединённая ячейка группы даёт значение только в первой колонке —
        # дальше оно тянется вручную, иначе «Цена» не с чем было бы связать.
        if top := _text(above[column] if column < len(above) else None):
            group = top
        own = _text(current[column] if column < len(current) else None)
        titles.append(" · ".join(p for p in (group, own) if p) if own else group)
    return titles


def _key_columns(
    titles: Sequence[str],
    rows: Sequence[Sequence[Any]],
    header: int,
) -> dict[str, int]:
    """Номера колонок артикула, названия, кода и штрихкода (1-based, 0 — нет)."""
    used: set[int] = set()
    found: dict[str, int] = {}
    for name, words in (
        ("article", _ARTICLE_WORDS),
        ("ean", _EAN_WORDS),
        ("sku", _SKU_WORDS),
        ("name", _NAME_WORDS),
    ):
        found[name] = _find_column(titles, words, rows, header, used)
        if found[name]:
            used.add(found[name])
    return found


def _find_column(
    titles: Sequence[str],
    words: Sequence[str],
    rows: Sequence[Sequence[Any]],
    header: int,
    used: set[int],
) -> int:
    """Колонка по подписи: сначала точное совпадение, потом вхождение слова.

    Порядок важен: рядом с «Товар» в шаблоне стоит «Уникальный идентификатор
    (Номенклатура)», и по одному лишь вхождению слова названием товара стал бы
    столбец с GUID — а из GUID не извлечь ни объём, ни название для сравнения.
    """
    scored: list[tuple[int, int, int]] = []
    for index, title in enumerate(titles):
        if index + 1 in used:
            continue
        normalized = normalize_text(title)
        if not any(word in normalized for word in words):
            continue
        if normalized in words:
            precision = 0
        elif any(normalized.startswith(word) for word in words):
            precision = 1
        else:
            precision = 2
        filled = _filled(rows, index, header + 1)
        if filled:
            scored.append((precision, -filled, index + 1))
    if not scored:
        return 0
    scored.sort()
    return scored[0][2]


def _filled(rows: Sequence[Sequence[Any]], column: int, start: int) -> int:
    return sum(
        1 for row in rows[start:]
        if column < len(row) and row[column] is not None and str(row[column]).strip()
    )


def _guid_columns(
    titles: Sequence[str],
    rows: Sequence[Sequence[Any]],
    header: int,
    types: Sequence[PriceType],
    columns: dict[str, int],
) -> tuple[int, int]:
    """Колонки с идентификаторами номенклатуры и характеристики (1-based).

    У каждого вида цены есть своя колонка «Уникальный идентификатор (Единица
    измерения)» с такой же подписью, поэтому по одному заголовку их не
    различить. Колонки видов цен уже разобраны, и они просто исключаются;
    из оставшихся берутся те, где значения действительно похожи на
    идентификатор.
    """
    taken = {number for number in columns.values() if number}
    for price_type in types:
        taken.update({price_type.old_column, price_type.percent_column,
                      price_type.price_column, price_type.unit_column,
                      price_type.unit_guid_column})

    found: list[tuple[int, str]] = []
    for index, title in enumerate(titles):
        number = index + 1
        if number in taken or not _looks_like_guid(rows, index, header + 1):
            continue
        found.append((number, normalize_text(title)))
    if not found:
        return 0, 0

    nomenclature = next(
        (n for n, t in found if any(w in t for w in _NOMENCLATURE_WORDS)), 0)
    characteristic = next(
        (n for n, t in found if n != nomenclature
         and any(w in t for w in _CHARACTERISTIC_WORDS)), 0)
    # Подписи может не быть вовсе — тогда порядок колонок в выгрузке 1С
    # постоянен: сначала номенклатура, следом характеристика.
    rest = [n for n, _ in found if n not in (nomenclature, characteristic)]
    if not nomenclature and rest:
        nomenclature = rest.pop(0)
    if not characteristic and rest:
        characteristic = rest.pop(0)
    return nomenclature, characteristic


def _looks_like_guid(rows: Sequence[Sequence[Any]], column: int, start: int) -> bool:
    """Похожа ли колонка на идентификатор: почти все значения нужного вида."""
    total = matched = 0
    for row in rows[start:]:
        if column >= len(row) or row[column] is None:
            continue
        total += 1
        if _GUID_RE.match(str(row[column]).strip()):
            matched += 1
    return total > 0 and matched / total >= 0.9


# --- виды цен -----------------------------------------------------------------

def _service_types(path: str, data_sheet: str) -> list[PriceType]:
    """Виды цен со служебного листа книги — там 1С перечисляет номера колонок."""
    for name in list_sheets(path):
        if name == data_sheet:
            continue
        try:
            rows, _ = read_raw(path, name, limit=200)
        except Exception:  # noqa: BLE001 — служебного листа может не быть вовсе
            continue
        if types := _parse_service_sheet(rows):
            return types
    return []


def _parse_service_sheet(rows: Sequence[Sequence[Any]]) -> list[PriceType]:
    if not rows:
        return []
    header = [normalize_text(v) for v in rows[0]]
    if not any(_SERVICE_MARK in title for title in header):
        return []

    mapping: dict[str, int] = {}
    name_column = guid_column = -1
    for index, title in enumerate(header):
        if not title:
            continue
        if _SERVICE_MARK in title:
            field_name = _service_field(title)
            if field_name and field_name not in mapping:
                mapping[field_name] = index
            continue
        if name_column < 0 and any(word in title for word in _TYPE_NAME_WORDS):
            name_column = index
        elif guid_column < 0 and any(word in title for word in _GUID_WORDS):
            guid_column = index

    if "price_column" not in mapping or name_column < 0:
        return []

    types: list[PriceType] = []
    for row in rows[1:]:
        name = _text(row[name_column] if name_column < len(row) else None)
        price = _number(row, mapping["price_column"])
        if not name or not price:
            continue
        price_type = PriceType(
            name=name,
            guid=_text(row[guid_column] if 0 <= guid_column < len(row) else None),
        )
        for field_name, index in mapping.items():
            setattr(price_type, field_name, _number(row, index))
        types.append(price_type)
    return types


def _service_field(title: str) -> str:
    for field_name, words in _SERVICE_FIELDS:
        if any(word in title for word in words):
            return field_name
    return ""


def _header_types(
    rows: Sequence[Sequence[Any]],
    header: int,
    titles: Sequence[str],
) -> list[PriceType]:
    """Запасной разбор: виды цен собираются по группам двухуровневой шапки."""
    above = rows[header - 1] if header > 0 else []
    current = rows[header] if header < len(rows) else []
    groups: dict[str, PriceType] = {}
    order: list[str] = []
    group = ""
    for index in range(len(titles)):
        if top := _text(above[index] if index < len(above) else None):
            group = top
        own = normalize_text(_text(current[index] if index < len(current) else None))
        if not own or not group:
            continue
        field_name = next(
            (name for name, words in _HEADER_FIELDS if any(w in own for w in words)), "")
        if not field_name:
            continue
        if group not in groups:
            groups[group] = PriceType(name=group)
            order.append(group)
        if not getattr(groups[group], field_name):
            setattr(groups[group], field_name, index + 1)
    return [groups[name] for name in order if groups[name].valid]


# --- строки -------------------------------------------------------------------

def _records(
    rows: Sequence[Sequence[Any]],
    header: int,
    columns: dict[str, int],
    progress=None,
) -> list[Record]:
    roles = [
        (columns["article"], FieldRole.ARTICLE),
        (columns["name"], FieldRole.NAME),
        (columns["sku"], FieldRole.SKU),
        (columns["ean"], FieldRole.EAN),
    ]
    records: list[Record] = []
    body = rows[header + 1:]
    total = len(body)
    for offset, row in enumerate(body):
        values = list(row)
        by_role = {
            role: values[number - 1]
            for number, role in roles
            if number and number - 1 < len(values) and values[number - 1] is not None
        }
        if not by_role.get(FieldRole.ARTICLE) and not by_role.get(FieldRole.NAME):
            continue
        records.append(Record(row=header + offset + 2, values=values, by_role=by_role))
        if progress and (offset % 250 == 0 or offset == total - 1):
            progress(offset + 1, total)
    return records


def _text(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split())


def _number(row: Sequence[Any], index: int) -> int:
    if index < 0 or index >= len(row):
        return 0
    try:
        return int(float(str(row[index]).strip()))
    except (TypeError, ValueError):
        return 0
