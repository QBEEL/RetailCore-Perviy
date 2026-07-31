"""Нормализация текста и величин. Не содержит привязки к конкретному бренду."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from .models import Quantity

# Единицы приводятся к базовым: мл, г, шт. Кириллица и латиница равноправны —
# именно их смешение ломало извлечение объёма в прежней версии.
_UNITS: dict[str, tuple[str, float]] = {
    "мл": ("мл", 1.0), "ml": ("мл", 1.0), "миллилитр": ("мл", 1.0),
    "л": ("мл", 1000.0), "l": ("мл", 1000.0), "литр": ("мл", 1000.0), "lt": ("мл", 1000.0),
    "г": ("г", 1.0), "гр": ("г", 1.0), "g": ("г", 1.0), "gr": ("г", 1.0), "грамм": ("г", 1.0),
    "кг": ("г", 1000.0), "kg": ("г", 1000.0), "килограмм": ("г", 1000.0),
    "мг": ("г", 0.001), "mg": ("г", 0.001),
    "шт": ("шт", 1.0), "штук": ("шт", 1.0), "pcs": ("шт", 1.0), "pc": ("шт", 1.0),
}

_UNIT_ALTERNATIVES = "|".join(sorted(_UNITS, key=len, reverse=True))
_QUANTITY_RE = re.compile(
    rf"(?<![\w,.])(\d+(?:[.,]\d+)?)\s*({_UNIT_ALTERNATIVES})(?![\w])",
    re.IGNORECASE,
)
_PARENTHESISED_RE = re.compile(r"\(([^)]*)\)")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_text(value: object) -> str:
    """Регистр, ё→е, схлопывание пробелов и пунктуации — основа всех сравнений."""
    if value is None:
        return ""
    text = str(value).casefold().replace("ё", "е")
    return " ".join(_WORD_RE.findall(text))


def tokenize(value: object) -> list[str]:
    return normalize_text(value).split()


def parse_quantity(value: object) -> Quantity | None:
    """Извлекает величину с единицей: «200 мл, банка», «212,5мл», «75гр» → Quantity."""
    if value is None:
        return None
    text = str(value)
    match = _QUANTITY_RE.search(text)
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    unit, factor = _UNITS[match.group(2).casefold()]
    return Quantity(number * factor, unit)


def extract_quantity(name: object) -> Quantity | None:
    """Ищет величину в названии, отдавая приоритет части в скобках: «... (10мл)»."""
    if name is None:
        return None
    text = str(name)
    for chunk in _PARENTHESISED_RE.findall(text):
        if quantity := parse_quantity(chunk):
            return quantity
    return parse_quantity(text)


def strip_quantity(name: object) -> str:
    """Убирает величину из названия, чтобы она не влияла на текстовое сходство."""
    if name is None:
        return ""
    text = _PARENTHESISED_RE.sub(lambda m: "" if parse_quantity(m.group(1)) else m.group(0), str(name))
    return _QUANTITY_RE.sub(" ", text)


def detect_noise_tokens(
    values: Iterable[object],
    *,
    threshold: float = 0.6,
    min_rows: int = 5,
) -> frozenset[str]:
    """Токены, встречающиеся почти в каждой строке, — это шум (бренд, тип прайса).

    Заменяет захардкоженную вырезку названия бренда: слово, присутствующее почти
    везде внутри файла, ничего не различает и только искажает сходство.
    """
    rows = 0
    counter: Counter[str] = Counter()
    for value in values:
        if not (unique := set(tokenize(value))):
            continue
        rows += 1
        counter.update(unique)
    if rows < min_rows:
        return frozenset()
    limit = rows * threshold
    return frozenset(token for token, count in counter.items() if count >= limit)


def comparable(value: object, noise: frozenset[str] = frozenset()) -> str:
    """Строка для сравнения названий: без величин, без шумовых токенов."""
    words = [w for w in tokenize(strip_quantity(value)) if w not in noise]
    return " ".join(words) or normalize_text(strip_quantity(value))


def digits_only(value: object) -> str:
    """Ключ для штрихкодов: EAN может прийти числом, строкой или с пробелами."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return "".join(ch for ch in text if ch.isdigit())


@dataclass(frozen=True, slots=True)
class KeyOptions:
    """Что именно не учитывается при сравнении кодов.

    Значения по умолчанию повторяют обычное поведение приложения: регистр,
    пробелы и знаки не значимы. Отдельные флаги нужны там, где пользователь
    сам решает строгость сравнения — например при переоценке, если у двух
    поставщиков артикулы различаются только регистром.
    """

    ignore_case: bool = True
    ignore_spaces: bool = True
    ignore_symbols: bool = True


DEFAULT_KEY_OPTIONS = KeyOptions()


def code_key(value: object, options: KeyOptions = DEFAULT_KEY_OPTIONS) -> str:
    """Ключ для артикулов: регистр и разделители по умолчанию не значимы."""
    if value is None:
        return ""
    text = str(value)
    if options.ignore_case:
        text = text.casefold()
    if options.ignore_symbols:
        return "".join(ch for ch in text if ch.isalnum())
    if options.ignore_spaces:
        return "".join(text.split())
    return text.strip()


def split_multi(value: object, separators: str = "/,;") -> list[str]:
    """Артикул может содержать несколько значений через «/», «,» или перенос строки."""
    if value is None:
        return []
    text = str(value).strip()
    if not text or text.startswith("="):
        return []
    pattern = f"[{re.escape(separators)}\n\r\t]" if separators else r"[\n\r\t]"
    return [part.strip() for part in re.split(pattern, text) if part.strip()]
