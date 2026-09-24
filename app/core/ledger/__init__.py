"""Ведомость по товарам на складах: разбор выгрузки 1С и разбор по полкам.

Выгрузка приходит нечитаемой не из-за оформления, а из-за формы: на каждый склад
по четыре колонки, складов шестнадцать, строки групп идут вперемешку с товарами,
итог стоит рядом с данными, а среди складов попадаются одноимённые и служебные
«в пути». Ответить по такой таблице на вопрос «чего не хватило» нельзя.

Разбор (`parse`), выводы (`analysis`) и оформление (`export`) разделены и
проверяются по отдельности. Главная проверка разбора — сверка с итоговой
колонкой самого файла: если сумма по складам с ней не сходится, шапку прочитали
неверно, и об этом нужно сказать, а не показывать красивые неверные числа.
"""
from __future__ import annotations

from .analysis import ItemTotal, Line, Report, STORE_TOP, TOP_LIMIT, build
from .export import default_name, save
from .models import Item, Ledger, Movement, Store
from .parse import ParseError, read

# Что открывается в диалоге выбора файла.
EXTENSIONS = ("*.xlsx", "*.xlsm")

__all__ = [
    "EXTENSIONS",
    "Item",
    "ItemTotal",
    "Ledger",
    "Line",
    "Movement",
    "ParseError",
    "Report",
    "STORE_TOP",
    "Store",
    "TOP_LIMIT",
    "build",
    "default_name",
    "read",
    "save",
]
