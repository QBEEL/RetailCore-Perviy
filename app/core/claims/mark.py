"""Код маркировки в заявке Торг-2: что в нём прочитать и когда он обязателен.

У бракованного или лишнего экземпляра маркируемого товара склад сканирует код
с упаковки. Из кода берётся всё, что он знает:

* штрих-код — GTIN, если не введён руками (у штучного товара это EAN-13);
* срок годности — поле AI 17, если оно в коде есть. Чаще его там нет, и тогда
  спрашивается «Честный ЗНАК» (нужен вход по сертификату на вкладке «Проверка
  кодов»), а у парфюмерии, у которой срока нет и в ответе системы, он считается
  от даты производства по правилам, заданным на вкладке сроков.

Модуль не зависит от Qt и сети: сеть вызывает окно, а здесь только разбор.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..marking import codes as codes_module
from ..marking import expiry
from .models import KINDS_WITH_ITEM


@dataclass(slots=True)
class MarkRead:
    """Что нашлось в отсканированном коде."""

    # Код в том виде, как его хранить: без пробелов по краям и в латинице, даже
    # если сканер набрал его в русской раскладке.
    code: str = ""
    valid: bool = False
    problems: list[str] = field(default_factory=list)
    gtin: str = ""
    # Штрих-код для поля заявки: EAN-13 у штучного товара, иначе GTIN как есть.
    barcode: str = ""
    expires: date | None = None
    produced: date | None = None
    name: str = ""
    brand: str = ""
    # Срок посчитан от даты производства, а не прочитан.
    estimated: bool = False
    shelf_years: int = 0
    # Срока нет, и его можно спросить у «Честного ЗНАКа».
    needs_lookup: bool = False

    @property
    def problem(self) -> str:
        return self.problems[0] if self.problems else ""


def needs_code(kind: str, marked: bool) -> bool:
    """Обязателен ли код: товар маркируемый и он физически есть на складе.

    У недопоставки товара нет — сканировать нечего, и требовать код значило бы
    не дать завести заявку вовсе.
    """
    return marked and kind in KINDS_WITH_ITEM


def read(text: str, years: int = expiry.DEFAULT_SHELF_YEARS,
         by_brand: dict[str, int] | None = None) -> MarkRead:
    """Разбирает код без обращения к сети. Не бросает: непонятное — в `problems`."""
    code = codes_module.normalize(text or "")
    result = MarkRead(code=code)
    if not code:
        return result
    parsed = codes_module.parse(code)
    result.valid = parsed.valid
    result.problems = list(parsed.problems)
    result.gtin = parsed.gtin
    result.barcode = parsed.ean13 or parsed.gtin
    if not parsed.valid:
        return result

    card = expiry.parse_card(code)
    expiry.apply_estimate(card, years, by_brand)
    _take(result, card)
    result.needs_lookup = card.expires is None
    return result


def complete(result: MarkRead, answer: Any, years: int = expiry.DEFAULT_SHELF_YEARS,
             by_brand: dict[str, int] | None = None) -> MarkRead:
    """Добавляет к прочитанному ответ ГИС МТ (`CodeInfo`): срок и название товара.

    Прочитанное из самого кода не затирается: ответ системы только дополняет.
    Если срока нет и в ответе, он считается от даты производства — тем же
    правилом, что и на вкладке сроков.
    """
    card = expiry.from_response(result.code, answer.raw)
    card.name = card.name or answer.product_name
    if result.expires is not None:
        card.expires, card.estimated = result.expires, result.estimated
    expiry.apply_estimate(card, years, by_brand)
    _take(result, card)
    result.name = result.name or answer.product_name or ""
    result.needs_lookup = False
    return result


def _take(result: MarkRead, card: expiry.Card) -> None:
    result.expires = card.expires
    result.produced = card.produced or result.produced
    result.name = card.name or result.name
    result.brand = card.brand or result.brand
    result.estimated = card.estimated
    result.shelf_years = card.shelf_years
