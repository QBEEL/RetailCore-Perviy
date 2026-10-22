"""Запас и заказ в Excel.

Первый лист — плоский список «что заказать», в том виде, в каком его принимает
вкладка «Заказ»: производитель, категория, артикул, номенклатура,
характеристика, штрихкод, статус и колонка «ИтогоЗаказ». Выбирать лист вручную
не нужно, а количества сразу переносятся в бланк поставщика.

Дальше — лист с выводами и параметрами, все товары и итоги по складам. Числа
выгружаются числами: по ним в Excel считают дальше.
"""
from __future__ import annotations

import os
import re

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from .. import sheets
from .analysis import STATES, Plan, Row

ORDER_COLUMN = "ИтогоЗаказ"

NOTES = (
    "Период выгрузки в отчёте 1С не записан — он указан вручную. Продажи в месяц = продажи "
    "за период ÷ число месяцев в нём. Позиция = остаток + товар в пути; резерв не "
    "вычитается. Хватит, мес. = позиция ÷ продажи в месяц. К заказу = продажи в месяц × "
    "срок заказа − позиция, вверх до штуки.",
    "Продажи взяты за весь период, а не за дни, когда товар был в наличии: если товар "
    "кончался, его спрос занижен. Товар без продаж может быть свежим приходом — даты "
    "прихода в отчёте нет. Кратность упаковки и минимальная партия не учитываются.",
)


def default_name(folder: str, plan: Plan, brand: str | None = None) -> str:
    who = re.sub(r'[<>:"/\\|?*]+', " ", brand or "все бренды").strip() or "все бренды"
    return os.path.join(folder, f"Запас и заказ — {who} {plan.params.end:%d.%m.%Y}.xlsx")


def save(plan: Plan, destination: str, brand: str | None = None) -> str:
    """Сохраняет расчёт. С брендом на листах только он — для заказа одному поставщику."""
    book = Workbook()
    book.remove(book.active)
    rows = plan.of_brand(brand)
    _order(book.create_sheet("К заказу"), plan.to_order(brand))
    _summary(book.create_sheet("Главное"), plan, rows, brand)
    _all(book.create_sheet("Все товары"), rows)
    _places(book.create_sheet("По складам"), plan)
    book.save(destination)
    return destination


# --- листы ---------------------------------------------------------------------

def _order(sheet: Worksheet, rows: list[Row]) -> None:
    row = sheets.head(sheet, 1, [
        "Производитель", "Категория", "Артикул", "Номенклатура", "Характеристика",
        "Штрихкод", "Статус", ORDER_COLUMN, "Остаток", "В пути", "Продажи за период",
        "Продажи в месяц", "Хватит, мес.", "Состояние"])
    for number, line in enumerate(rows):
        item = line.item
        sheets.row(sheet, row, [
            item.brand, item.category, item.article, item.name, item.trait, item.barcode,
            item.status, line.qty, item.stock, item.transit, item.sales, line.per_month,
            line.cover, line.state], formats="ttttttnnnnnmmt", stripe=number % 2 == 1)
        row += 1
    sheets.widths(sheet, 1)


def _summary(sheet: Worksheet, plan: Plan, rows: list[Row], brand: str | None) -> None:
    params = plan.params
    row = sheets.title(
        sheet, 1, f"Запас и заказ · {brand or 'все бренды'} · {params.title}", 6)
    for text in NOTES:
        row = sheets.note(sheet, row, text, 6)
        sheet.row_dimensions[row - 1].height = 52
    row += 1

    ordered = [line for line in rows if line.qty > 0]
    counts = plan.counts(rows)
    values: list[tuple[str, object, str]] = [
        ("Период", params.title, "t"),
        ("Дней в периоде", params.days, "n"),
        ("Месяцев в периоде", params.months, "m"),
        ("Срок заказа, мес.", params.target, "m"),
        ("«Мало», если хватит меньше, мес.", params.low, "m"),
        ("«Избыток», если хватит больше, мес.", params.high, "m"),
        ("Товаров в отчёте", len(rows), "n"),
        ("Позиций к заказу", len(ordered), "n"),
        ("Штук к заказу", sum(line.qty for line in ordered), "n"),
        ("Из них поправлено вручную", sum(1 for line in ordered if line.changed), "n"),
        ("Остаток, шт", sum(line.item.stock for line in rows), "n"),
        ("В пути, шт", sum(line.item.transit for line in rows), "n"),
        ("Продажи за период, шт", sum(line.item.sales for line in rows), "n"),
    ]
    row = sheets.head(sheet, row, ["Показатель", "Значение"])
    for number, (name, value, kind) in enumerate(values):
        sheets.row(sheet, row, [name, value], formats="t" + kind, stripe=number % 2 == 1)
        row += 1

    row += 1
    row = sheets.head(sheet, row, ["Состояние", "Товаров"])
    for number, state in enumerate(STATES):
        sheets.row(sheet, row, [state, counts[state]], formats="tn", stripe=number % 2 == 1)
        row += 1

    groups = [group for group in plan.brands() if not brand or group.name == brand]
    if len(groups) > 1:
        row += 1
        row = sheets.head(sheet, row, [
            "Бренд", "Товаров", "Остаток", "В пути", "Продажи", "Позиций к заказу",
            "Штук к заказу"])
        for number, group in enumerate(groups):
            sheets.row(sheet, row, [
                group.name, group.items, group.stock, group.transit, group.sales,
                group.ordered_items, group.order], formats="tnnnnnn", stripe=number % 2 == 1)
            row += 1

    for text in plan.warnings + plan.stock.warnings:
        row += 1
        row = sheets.note(sheet, row, text, 6)
    sheets.widths(sheet, 1)
    sheet.column_dimensions["A"].width = 38


def _all(sheet: Worksheet, rows: list[Row]) -> None:
    row = sheets.head(sheet, 1, [
        "Производитель", "Категория", "Артикул", "Номенклатура", "Характеристика",
        "Штрихкод", "Статус", "Остаток", "В пути", "Продажи за период", "Продажи в месяц",
        "Хватит, мес.", "Состояние", "Расчёт заказа", "ИтогоЗаказ", "Рекомендует 1С"])
    for number, line in enumerate(rows):
        item = line.item
        sheets.row(sheet, row, [
            item.brand, item.category, item.article, item.name, item.trait, item.barcode,
            item.status, item.stock, item.transit, item.sales, line.per_month, line.cover,
            line.state, line.order, line.qty, item.recommended],
            formats="ttttttnnnnmmtnnn", stripe=number % 2 == 1, warn=line.changed)
        row += 1
    sheets.widths(sheet, 1)


def _places(sheet: Worksheet, plan: Plan) -> None:
    row = sheets.head(sheet, 1, [
        "Склад / магазин", "Остаток", "В пути", "Продажи за период", "Товаров с продажами",
        "Кончилось, а продажи были", "Хватит, мес."])
    for number, place in enumerate(plan.places):
        sheets.row(sheet, row, [
            place.name, place.stock, place.transit, place.sales if place.selling else None,
            place.sold_items if place.selling else None,
            place.missing_items if place.selling else None, place.cover],
            formats="tnnnnnm", stripe=number % 2 == 1)
        row += 1
    sheets.widths(sheet, 1)
