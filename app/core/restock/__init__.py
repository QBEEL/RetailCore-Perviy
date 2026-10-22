"""Запас и заказ: сколько хватит товара и сколько заказать.

Исходник — отчёт 1С по брендам: по каждому товару продажи и остатки на каждом
складе. Периода в нём нет, его указывает человек, и от него зависят месячные
продажи, запас в месяцах и заказ.

Разбор (`parse`), расчёт (`analysis`) и оформление (`export`) разделены и
проверяются по отдельности, как у ведомости по складам и аналитики по чекам.
"""
from __future__ import annotations

from .analysis import (
    DEAD,
    DEFAULT_HIGH,
    DEFAULT_LOW,
    DEFAULT_TARGET,
    HIGH,
    IDLE,
    LOW,
    OK,
    OUT,
    STATES,
    Group,
    Params,
    Plan,
    PlaceSummary,
    Row,
    build,
    calculate,
)
from .export import default_name, save
from .models import Item, Place, Stock
from .parse import ParseError, parse_rows, read

# Что открывается в диалоге выбора файла.
EXTENSIONS = ("*.xlsx", "*.xlsm")

__all__ = [
    "DEAD",
    "DEFAULT_HIGH",
    "DEFAULT_LOW",
    "DEFAULT_TARGET",
    "EXTENSIONS",
    "Group",
    "HIGH",
    "IDLE",
    "Item",
    "LOW",
    "OK",
    "OUT",
    "ParseError",
    "Params",
    "Place",
    "PlaceSummary",
    "Plan",
    "Row",
    "STATES",
    "Stock",
    "build",
    "calculate",
    "default_name",
    "parse_rows",
    "read",
    "save",
]
