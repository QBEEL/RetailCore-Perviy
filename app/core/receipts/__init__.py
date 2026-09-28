"""Аналитика по чекам: разбор выгрузки 1С и срезы продаж.

Выгрузка — строка на каждый товар в чеке: магазин, гость, бренд, продавец,
скидки, бонусы и себестоимость. Над таблицей 1С печатает период, и он здесь
главный параметр: без него выручка не отвечает на вопрос «за что».

Разбор (`parse`), сведение нескольких файлов (`merge`), выводы (`analysis`) и
оформление (`export`) разделены и проверяются по отдельности, как у ведомости
по складам. Главная проверка разбора — сверка со строкой «Итого» самого файла.
"""
from __future__ import annotations

from .analysis import Group, Report, build
from .export import default_name, save
from .merge import merge, read_many
from .models import Line, Receipts
from .parse import ParseError, parse_rows, read

# Что открывается в диалоге выбора файла.
EXTENSIONS = ("*.xlsx", "*.xlsm")

__all__ = [
    "EXTENSIONS",
    "Group",
    "Line",
    "ParseError",
    "Receipts",
    "Report",
    "build",
    "default_name",
    "merge",
    "parse_rows",
    "read",
    "read_many",
    "save",
]
