"""Разбор отчёта 1С по брендам: товары, остатки и продажи по складам.

Шапка двухэтажная: слева общие колонки (производитель, артикул, продажи…), дальше
по блоку на каждый склад — «Остаток на складе», «Продажи», «Резерв», «В пути»,
«Свободный остаток», «Заказ». Название склада стоит над блоком один раз, в
объединённой ячейке, поэтому оно протягивается вправо до следующего названия.

Строки идут уровнями: итог бренда, итог категории, товары. Товар узнаётся по
номенклатуре — у части товаров нет ни артикула, ни штрихкода. Итоги брендов и
категорий не считаются товарами, но по ним проверяется разбор: если товары бренда
не дали его итог, шапку прочитали неверно, и сказать об этом важнее, чем показать
отчёт.

Период выгрузки в файле не записан, поэтому здесь его нет: он задаётся там, где
считается запас в месяцах.
"""
from __future__ import annotations

import os
from typing import Any

from ..workbook import list_sheets, read_raw
from .models import Item, Place, Stock

# Допуск при сверке с итогами: 1С хранит остатки дробными, но штуки целые.
TOLERANCE = 0.5

# Докуда искать шапку. В отчёте она первая, но в файле, который открывали и
# дополняли руками, над ней может появиться пара строк.
SEARCH_DEPTH = 40

NO_BRAND = "Без бренда"

BRAND, CATEGORY, ARTICLE, NAME, TRAIT, BARCODE = (
    "brand", "category", "article", "name", "trait", "barcode")
STATUS, STOCK, SALES, AMOUNT, RECOMMENDED = (
    "status", "stock", "sales", "amount", "recommended")
RESERVE, TRANSIT, FREE = "reserve", "transit", "free"

# Подписи после `_key`: без регистра, знаков и пробелов.
COMMON = {
    "производитель": BRAND, "бренд": BRAND, "марка": BRAND,
    "категория": CATEGORY, "категориятовара": CATEGORY,
    "артикул": ARTICLE,
    "номенклатура": NAME, "товар": NAME, "наименование": NAME,
    "характеристика": TRAIT,
    "штрихкод": BARCODE,
    "статус": STATUS,
    "текущийостаток": STOCK,
    "продажи": SALES,
    "суммапродаж": AMOUNT,
    "рекомендовано": RECOMMENDED,
}

PLACE = {
    "остатокнаскладе": STOCK, "остаток": STOCK,
    "продажи": SALES,
    "резерв": RESERVE,
    "впути": TRANSIT,
    "свободныйостаток": FREE,
}

# По скольким из этих подписей строка опознаётся как шапка.
_HEAD_WORDS = {"производитель", "категория", "артикул", "номенклатура", "штрихкод",
               "статус", "текущийостаток", "продажи"}


class ParseError(Exception):
    """Файл не похож на отчёт по брендам. Текст пригоден для показа."""


def _text(value: Any) -> str:
    return " ".join(str(value).split()).strip() if value is not None else ""


def _key(value: Any) -> str:
    text = _text(value).lower().replace("ё", "е")
    return "".join(ch for ch in text if ch.isalnum())


def _number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def _code(value: Any) -> str:
    """Артикул или штрихкод: число из Excel не должно стать «4640470860078.0»."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _text(value)


def _filled(value: Any) -> bool:
    return value is not None and _text(value) != ""


def _head_row(rows: list[list[Any]]) -> int | None:
    for index, row in enumerate(rows[:SEARCH_DEPTH]):
        keys = {_key(cell) for cell in row if _filled(cell)}
        if "номенклатура" in keys and len(keys & _HEAD_WORDS) >= 3:
            return index
    return None


class _Columns:
    """Где в строке что лежит."""

    def __init__(self, head: list[Any], sub: list[Any]) -> None:
        self.common: dict[str, int] = {}
        self.places: list[tuple[str, dict[str, int]]] = []
        group: dict[str, int] | None = None
        previous_in_group = False
        top = ""
        used: dict[str, int] = {}
        for index in range(max(len(head), len(sub))):
            title = _text(head[index]) if index < len(head) else ""
            low = _text(sub[index]) if index < len(sub) else ""
            if title:
                top = title
            elif not low or not previous_in_group:
                top = ""
            if low:
                role = PLACE.get(_key(low))
                if title or group is None or not previous_in_group:
                    # Новый блок. Одноимённые склады разводятся номером: сложить
                    # два склада в один — потерять данные одного из них.
                    count = used.get(top, 0) + 1
                    used[top] = count
                    name = top if count == 1 else f"{top} ({count})"
                    group = {}
                    self.places.append((name, group))
                if role and role not in group:
                    group[role] = index
                previous_in_group = True
            else:
                previous_in_group = False
                group = None
                role = COMMON.get(_key(top))
                if role and role not in self.common:
                    self.common[role] = index


def _cell(row: list[Any], column: int | None) -> Any:
    if column is None or column >= len(row):
        return None
    return row[column]


def parse_rows(rows: list[list[Any]], *, sheet: str = "", source: str = "") -> Stock:
    head = _head_row(rows)
    if head is None:
        raise ParseError(
            "Не нашлась шапка отчёта по брендам: нужны колонки «Номенклатура», "
            "«Артикул», «Продажи» и остатки по складам. Выгрузите отчёт, в котором "
            "они есть, и выберите файл снова.")
    sub_row = rows[head + 1] if head + 1 < len(rows) else []
    has_sub = any(_key(cell) in PLACE for cell in sub_row if _filled(cell))
    columns = _Columns(rows[head], sub_row if has_sub else [])
    first = head + (2 if has_sub else 1)

    common = columns.common
    places = [(name, group) for name, group in columns.places if group.get(STOCK) is not None
              or group.get(SALES) is not None]
    if NAME not in common:
        raise ParseError("В отчёте нет колонки «Номенклатура».")
    have_stock = STOCK in common or any(STOCK in group for _, group in places)
    have_sales = SALES in common or any(SALES in group for _, group in places)
    if not have_stock or not have_sales:
        missing = [title for title, ok in (("остатки", have_stock), ("продажи", have_sales))
                   if not ok]
        raise ParseError("В отчёте нет колонок: " + ", ".join(missing)
                         + ". Они нужны, чтобы посчитать запас.")

    result = Stock(sheet=sheet, source=source)
    result.places = [name for name, _ in places]
    result.selling = [name for name, group in places if SALES in group]

    brand_totals: dict[str, tuple[float, float]] = {}
    grand: tuple[float, float] | None = None
    stock_mismatch = 0
    sales_mismatch = 0

    for offset, row in enumerate(rows[first:]):
        number = first + offset + 1
        if not any(_filled(cell) for cell in row):
            continue
        name = _text(_cell(row, common.get(NAME)))
        brand = _text(_cell(row, common.get(BRAND)))
        category = _text(_cell(row, common.get(CATEGORY)))

        stock_total = _number(_cell(row, common.get(STOCK)))
        sales_total = _number(_cell(row, common.get(SALES)))
        built: list[Place] = []
        for place_name, group in places:
            place = Place(
                place_name,
                stock=_number(_cell(row, group.get(STOCK))),
                sales=_number(_cell(row, group.get(SALES))),
                reserve=_number(_cell(row, group.get(RESERVE))),
                transit=_number(_cell(row, group.get(TRANSIT))),
                free=_number(_cell(row, group.get(FREE))),
            )
            if place.stock or place.sales or place.reserve or place.transit or place.free:
                built.append(place)
        if STOCK not in common:
            stock_total = sum(place.stock for place in built)
        if SALES not in common:
            sales_total = sum(place.sales for place in built)

        first_cells = [_key(cell) for cell in row[:3]]
        if "итого" in first_cells and not name:
            grand = (stock_total, sales_total)
            continue
        if not name:
            # Строка группы. Итог бренда — без категории; итоги категорий нужны
            # только 1С, товары сводятся и без них.
            if brand and not category:
                brand_totals[brand] = (stock_total, sales_total)
            continue

        if built and STOCK in common and abs(
                sum(p.stock for p in built) - stock_total) > TOLERANCE:
            stock_mismatch += 1
        if built and SALES in common and abs(
                sum(p.sales for p in built) - sales_total) > TOLERANCE:
            sales_mismatch += 1
        recommended = _cell(row, common.get(RECOMMENDED))
        result.items.append(Item(
            brand=brand or NO_BRAND,
            category=category,
            article=_code(_cell(row, common.get(ARTICLE))),
            name=name,
            trait=_text(_cell(row, common.get(TRAIT))),
            barcode=_code(_cell(row, common.get(BARCODE))),
            status=_text(_cell(row, common.get(STATUS))),
            stock=stock_total,
            sales=sales_total,
            amount=_number(_cell(row, common.get(AMOUNT))),
            recommended=_number(recommended) if _filled(recommended) else None,
            places=built,
            row=number,
        ))

    if not result.items:
        raise ParseError("В отчёте нашлась шапка, но не нашлось ни одного товара.")
    _verify(result, brand_totals, grand, stock_mismatch, sales_mismatch)
    return result


def _verify(result: Stock, brand_totals: dict[str, tuple[float, float]],
            grand: tuple[float, float] | None, stock_mismatch: int,
            sales_mismatch: int) -> None:
    sums: dict[str, list[float]] = {}
    for item in result.items:
        total = sums.setdefault(item.brand, [0.0, 0.0])
        total[0] += item.stock
        total[1] += item.sales

    checked = False
    for brand, (stock, sales) in brand_totals.items():
        if brand not in sums:
            continue
        checked = True
        got = sums[brand]
        if abs(got[0] - stock) > TOLERANCE or abs(got[1] - sales) > TOLERANCE:
            result.balanced = False
            result.warnings.append(
                f"{brand}: товары дали остаток {got[0]:,.0f} и продажи {got[1]:,.0f}, "
                f"а итог бренда в файле — {stock:,.0f} и {sales:,.0f}".replace(",", " "))
    if grand is not None:
        checked = True
        stock = sum(total[0] for total in sums.values())
        sales = sum(total[1] for total in sums.values())
        if abs(stock - grand[0]) > TOLERANCE or abs(sales - grand[1]) > TOLERANCE:
            result.balanced = False
            result.warnings.append("Товары не сошлись со строкой «Итого» файла")
    if checked and result.balanced is None:
        result.balanced = True
    if stock_mismatch:
        result.warnings.append(
            f"У {stock_mismatch} товаров «ТекущийОстаток» не равен сумме по складам: остатки "
            "по магазинам и итог по сети расходятся")
    if sales_mismatch:
        # Бывает, когда магазин, в котором торговали, в колонки отчёта не выведен.
        # Расчёт по сети идёт по итогу, по магазинам — по тому, что показано.
        result.warnings.append(
            f"У {sales_mismatch} товаров итог продаж больше (или меньше) суммы по "
            "показанным в отчёте магазинам — вероятно, часть магазинов в отчёт не выведена. "
            "Расчёт по сети идёт по итогу")


def read(path: str, sheet: str | None = None) -> Stock:
    """Читает отчёт. Без листа берётся первый, где нашёлся отчёт по брендам."""
    names = [sheet] if sheet else list_sheets(path)
    first_error: ParseError | None = None
    for name in names:
        rows, resolved = read_raw(path, name)
        try:
            return parse_rows(rows, sheet=resolved, source=os.path.basename(path))
        except ParseError as error:
            first_error = first_error or error
    raise first_error or ParseError("В файле нет листов.")
