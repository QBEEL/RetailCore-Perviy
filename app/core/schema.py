"""Автоопределение строки заголовка и семантических ролей колонок."""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Sequence

from .models import Column, FieldRole
from .normalize import normalize_text

# Синонимы заголовков. Порядок важен: более специфичные роли проверяются раньше,
# чтобы «Цена» не поглотила «Количество», а «Штрихкод» — «Код».
_HEADER_SYNONYMS: tuple[tuple[FieldRole, tuple[str, ...]], ...] = (
    (FieldRole.EAN, ("ean", "штрихкод", "штрих код", "штрих", "barcode", "bar code", "gtin", "upc")),
    (FieldRole.ARTICLE, ("артикул", "article", "vendor code")),
    (FieldRole.MANUFACTURER, ("производитель", "изготовитель", "поставщик", "manufacturer", "vendor", "supplier")),
    (FieldRole.QUANTITY, ("количество", "кол во", "колво", "остаток", "quantity", "qty", "stock")),
    (FieldRole.PRICE, ("ррц", "rrc", "цена", "стоимость", "price", "розничная", "msrp")),
    (FieldRole.VOLUME, ("объем", "фасовка", "емкость", "вес", "volume", "capacity", "weight", "нетто")),
    (FieldRole.SIZE, ("размер", "size")),
    (FieldRole.COLOR, ("цвет", "оттенок", "color", "colour", "shade")),
    (FieldRole.BRAND, ("бренд", "brand", "торговая марка", "тм", "trademark")),
    (FieldRole.CATEGORY, ("категория", "category", "группа", "раздел", "тип товара", "коллекция")),
    (FieldRole.DATE, ("дата", "срок годности", "срок", "date", "expiry", "годности")),
    (FieldRole.NOTE, ("примечание", "комментарий", "note", "comment", "remark")),
    (FieldRole.DESCRIPTION, ("описание", "description", "состав", "ingredients")),
    (FieldRole.SKU, ("sku", "код товара", "код номенклатуры", "код")),
    (FieldRole.NAME, ("наименование", "номенклатура", "название", "продукт", "товар", "name", "product", "item")),
)

_ERROR_VALUES = frozenset({"#value!", "#n/a", "#ref!", "#name?", "#div/0!", "#null!", "#num!"})
_EAN_RE = re.compile(r"^\d{8}$|^\d{12,14}$")
# Роль назначается одной колонке; остальные совпадения понижаются до дублирующей роли.
_DUPLICATE_FALLBACK: dict[FieldRole, FieldRole] = {
    FieldRole.NAME: FieldRole.NAME_ALT,
    FieldRole.ARTICLE: FieldRole.SKU,
}


def clean_value(value: Any) -> Any:
    """Отбрасывает формулы и значения-ошибки Excel, оставляя данные."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("=") or text.casefold() in _ERROR_VALUES:
            return None
        return text or None
    return value


def detect_header_row(rows: Sequence[Sequence[Any]], limit: int = 12) -> int:
    """Заголовок — строка с наибольшим числом коротких текстовых подписей."""
    best_row, best_score = 0, -1.0
    for index, row in enumerate(rows[:limit]):
        labels = [v for v in row if isinstance(v, str) and v.strip() and len(v.strip()) <= 60]
        if not labels:
            continue
        numeric = sum(1 for v in row if isinstance(v, (int, float)) and not isinstance(v, bool))
        score = len(labels) - numeric * 0.5
        if score > best_score:
            best_row, best_score = index, score
    return best_row


def _role_from_header(title: str) -> tuple[FieldRole, float]:
    normalized = normalize_text(title)
    if not normalized:
        return FieldRole.OTHER, 0.0
    for role, synonyms in _HEADER_SYNONYMS:
        for synonym in synonyms:
            if normalized == synonym:
                return role, 1.0
            if synonym in normalized:
                return role, 0.8
    return FieldRole.OTHER, 0.0


def _role_from_content(values: Sequence[Any]) -> tuple[FieldRole, float]:
    """Резервное определение по данным, когда заголовок отсутствует или невнятен."""
    sample = [v for v in values if v is not None][:200]
    if not sample:
        return FieldRole.OTHER, 0.0
    if sum(1 for v in sample if isinstance(v, dt.datetime)) / len(sample) > 0.7:
        return FieldRole.DATE, 0.7
    codes = sum(1 for v in sample if _EAN_RE.match(str(v).removesuffix(".0").strip()))
    if codes / len(sample) > 0.7:
        return FieldRole.EAN, 0.75
    return FieldRole.OTHER, 0.0


def detect_columns(header: Sequence[Any], body: Sequence[Sequence[Any]]) -> list[Column]:
    """Строит список колонок с ролями: сначала по заголовкам, затем по содержимому."""
    width = max([len(header), *(len(r) for r in body)] or [0])
    columns: list[Column] = []
    for index in range(width):
        raw_title = header[index] if index < len(header) else None
        title = str(raw_title).strip() if raw_title is not None else ""
        role, confidence = _role_from_header(title)
        if role is FieldRole.OTHER:
            role, confidence = _role_from_content([r[index] if index < len(r) else None for r in body])
        columns.append(Column(index=index, title=title or _fallback_title(index), role=role, confidence=confidence))

    _resolve_duplicates(columns)
    _ensure_name_column(columns, body)
    return columns


def _fallback_title(index: int) -> str:
    return f"Колонка {Column(index=index, title='').letter}"


def _resolve_duplicates(columns: list[Column]) -> None:
    """Оставляет одну колонку на роль: «Наименование английское» становится доп. полем."""
    seen: set[FieldRole] = set()
    for column in sorted(columns, key=lambda c: (-c.confidence, c.index)):
        if column.role is FieldRole.OTHER:
            continue
        if column.role not in seen:
            seen.add(column.role)
            continue
        fallback = _DUPLICATE_FALLBACK.get(column.role, FieldRole.OTHER)
        column.role = FieldRole.OTHER if fallback in seen else fallback
        if column.role is not FieldRole.OTHER:
            seen.add(column.role)


def _ensure_name_column(columns: list[Column], body: Sequence[Sequence[Any]]) -> None:
    """Без названия сопоставление бессмысленно — берём самую «текстовую» колонку."""
    if any(c.role is FieldRole.NAME for c in columns):
        return
    best, best_length = None, 0.0
    for column in columns:
        if column.role is not FieldRole.OTHER:
            continue
        texts = [str(r[column.index]) for r in body if column.index < len(r) and isinstance(r[column.index], str)]
        if len(texts) < max(1, len(body) * 0.5):
            continue
        average = sum(len(t) for t in texts) / len(texts)
        if average > best_length:
            best, best_length = column, average
    if best is not None and best_length >= 8:
        best.role = FieldRole.NAME
        best.confidence = 0.5
