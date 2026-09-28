"""Аналитика по чекам в Excel: лист с выводами и по листу на срез.

Исходная выгрузка — пять тысяч строк «товар в чеке», по которым ответа на
вопрос «какой магазин приносит больше и за счёт чего» не видно. Здесь те же
данные разложены по вопросам: магазины, бренды, категории, товары, продавцы,
дни, часы, акции и возвраты.

Суммы выгружаются числами, а не текстом: по ним в Excel считают дальше.
"""
from __future__ import annotations

import os
from datetime import date

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from .. import sheets
from .analysis import Group, Report, weekday


def save(report: Report, destination: str) -> str:
    book = Workbook()
    book.remove(book.active)
    _summary(book.create_sheet("Главное"), report)
    _groups(book.create_sheet("Магазины"), report, report.stores, "Магазин")
    _groups(book.create_sheet("Бренды"), report, report.brands, "Бренд")
    _groups(book.create_sheet("Категории"), report, report.categories, "Категория")
    _items(book.create_sheet("Товары"), report)
    _sellers(book.create_sheet("Продавцы"), report)
    _days(book.create_sheet("По дням"), report)
    _hours(book.create_sheet("По часам"), report)
    if report.promos:
        _groups(book.create_sheet("Акции"), report, report.promos, "Акция")
    if report.returns:
        _returns(book.create_sheet("Возвраты"), report)
    book.save(destination)
    return destination


def default_name(folder: str, report: Report | None = None) -> str:
    receipts = report.receipts if report is not None else None
    if receipts is not None and receipts.start and receipts.end:
        span = f"{receipts.start:%d.%m.%Y}–{receipts.end:%d.%m.%Y}"
    else:
        span = f"{date.today():%Y-%m-%d}"
    return os.path.join(folder, f"Аналитика по чекам — разбор {span}.xlsx")


# --- листы ---------------------------------------------------------------------

def _summary(sheet: Worksheet, report: Report) -> None:
    receipts = report.receipts
    total = report.total
    row = sheets.title(sheet, 1, f"Аналитика по чекам · {receipts.period_title}", 4)
    row = sheets.note(
        sheet, row,
        "Выручка — «Сумма» файла: после скидок, возвраты со своим минусом. "
        "Средний чек и товаров в чеке — только по продажам: возврат занизил бы "
        "их ровно на сумму возврата. Скидка — доля от суммы до скидки.", 4)
    row += 1
    has_cost = receipts.has("cost")
    values: list[tuple[str, object, str]] = [
        ("Период", receipts.period_title, "t"),
        ("Дней в периоде", receipts.days or None, "n"),
        ("Файлов", len(receipts.sources) or 1, "n"),
        ("Выручка", total.revenue, "m"),
        ("Выручка в день", report.per_day or None, "m"),
        ("Себестоимость", total.cost if has_cost else None, "m"),
        ("Валовая прибыль", total.profit if has_cost else None, "m"),
        ("Маржа", total.margin if has_cost else None, "%"),
        ("Чеков", total.checks, "n"),
        ("Средний чек", total.average_check, "m"),
        ("Товаров в чеке", total.per_check, "m"),
        ("Продано, шт", total.quantity, "n"),
        ("Скидки всего", total.discount, "m"),
        ("Скидки, доля", total.discount_share, "%"),
        ("Начислено бонусов", total.bonus_accrued, "m"),
        ("Возвраты", total.returned, "m"),
        ("Гостей с картой", report.guests, "n"),
        ("Из них приходили повторно", report.repeat_guests, "n"),
        ("Покупки без продавца", report.share(report.self_service), "%"),
    ]
    row = sheets.head(sheet, row, ["Показатель", "Значение"])
    for number, (name, value, kind) in enumerate(values):
        sheets.row(sheet, row, [name, value], formats="t" + kind, stripe=number % 2 == 1)
        row += 1

    if len(report.kinds) > 1:
        row += 1
        row = sheets.head(sheet, row, ["Вид документа", "Документов", "Сумма"])
        for number, group in enumerate(report.kinds):
            sheets.row(sheet, row, [group.name, len(group.documents), group.revenue],
                       formats="tnm", stripe=number % 2 == 1)
            row += 1

    for text in ("Подразделения: " + receipts.units if receipts.units else "",
                 "Отбор: " + receipts.selection if receipts.selection else "",
                 *receipts.warnings):
        if text:
            row += 1
            row = sheets.note(sheet, row, text, 4)
    sheets.widths(sheet, 1)


def _metrics(report: Report, group: Group) -> tuple[list, str]:
    """Общие колонки среза: выручка, доля, прибыль, чеки, средний чек, скидка."""
    return ([group.revenue, report.share(group), group.profit, group.margin,
             group.checks, group.average_check, group.quantity, group.discount_share,
             group.returned or None],
            "m%m%nmn%m")


_METRIC_TITLES = ["Выручка", "Доля выручки", "Валовая прибыль", "Маржа", "Чеков",
                  "Средний чек", "Продано, шт", "Скидка", "Возвраты"]


def _groups(sheet: Worksheet, report: Report, groups: list[Group], title: str) -> None:
    row = sheets.head(sheet, 1, [title, *_METRIC_TITLES])
    for number, group in enumerate(groups):
        values, formats = _metrics(report, group)
        sheets.row(sheet, row, [group.name, *values], formats="t" + formats,
                   stripe=number % 2 == 1)
        row += 1
    sheets.widths(sheet, 1)


def _items(sheet: Worksheet, report: Report) -> None:
    row = sheets.head(sheet, 1, ["Товар", "Бренд", "Категория", *_METRIC_TITLES,
                                 "Магазинов"])
    for number, group in enumerate(report.items):
        values, formats = _metrics(report, group)
        sheets.row(sheet, row, [group.name, group.brand, group.category, *values,
                                len(group.stores)],
                   formats="ttt" + formats + "n", stripe=number % 2 == 1)
        row += 1
    sheets.widths(sheet, 1)


def _sellers(sheet: Worksheet, report: Report) -> None:
    row = sheets.head(sheet, 1, ["Продавец", *_METRIC_TITLES, "Магазины"])
    groups = [*report.sellers]
    if report.self_service.lines:
        groups.append(report.self_service)
    for number, group in enumerate(groups):
        values, formats = _metrics(report, group)
        sheets.row(sheet, row, [group.name, *values, ", ".join(sorted(group.stores))],
                   formats="t" + formats + "t", stripe=number % 2 == 1)
        row += 1
    sheets.widths(sheet, 1)


def _days(sheet: Worksheet, report: Report) -> None:
    row = sheets.head(sheet, 1, ["Дата", "День", "Выручка", "Чеков", "Средний чек",
                                 "Продано, шт", "Валовая прибыль"])
    for number, (day, group) in enumerate(report.days):
        sheets.row(sheet, row, [day.strftime("%d.%m.%Y"), weekday(day), group.revenue,
                                group.checks, group.average_check, group.quantity,
                                group.profit],
                   formats="ttmnmnm", stripe=day.weekday() >= 5)
        row += 1
    sheets.widths(sheet, 1)


def _hours(sheet: Worksheet, report: Report) -> None:
    row = sheets.head(sheet, 1, ["Час", "Чеков", "Доля чеков", "Выручка", "Средний чек"])
    checks = report.total.checks or 1
    for number, (hour, group) in enumerate(report.hours):
        sheets.row(sheet, row, [f"{hour:02d}:00–{hour:02d}:59", group.checks,
                                group.checks / checks, group.revenue, group.average_check],
                   formats="tn%mm", stripe=number % 2 == 1)
        row += 1
    sheets.widths(sheet, 1)


def _returns(sheet: Worksheet, report: Report) -> None:
    row = sheets.head(sheet, 1, ["Дата", "Документ", "Магазин", "Товар", "Бренд",
                                 "Количество", "Сумма", "Гость", "Продавец"])
    for number, line in enumerate(report.returns):
        sheets.row(sheet, row, [line.day.strftime("%d.%m.%Y") if line.day else "",
                                line.document, line.store, line.item, line.brand,
                                line.quantity, line.amount, line.guest, line.seller],
                   formats="tttttnmtt", stripe=number % 2 == 1)
        row += 1
    sheets.widths(sheet, 1)
