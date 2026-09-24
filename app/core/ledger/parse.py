"""Разбор выгрузки «Ведомость по товарам на складах» из 1С.

Шапка ищется по подписям, а не по номерам колонок. Состав складов меняется от
выгрузки к выгрузке — магазин открыли, склад переименовали, — и отчёт, в котором
колонки посчитаны от буквы «H», сломается на первой же такой выгрузке молча:
числа встанут не под тем складом, и заметить это будет нечем.

Строку товара от строки группы отличает единица измерения. У групп её нет, а
числа в них — итоги вложенных товаров: сложить их вместе с товарами значило бы
посчитать всё дважды.
"""
from __future__ import annotations

from typing import Any

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from .models import Item, Ledger, Movement, Store

# Подписи шапки. Ищутся без учёта регистра и лишних пробелов: в выгрузках
# встречается и «Ед. изм.», и «Ед.изм».
ARTICLE = "артикул"
NAME = "номенклатура"
# Точки выброшены нормализацией: в выгрузках встречается «Ед. изм.», «Ед.изм.»
# и «Ед.изм» — это одна и та же колонка.
UNIT = "ед изм"
TOTAL = "итого"

# Подписи четвёрки колонок склада. Порядок читается из файла, а не считается
# заданным: перестановка колонок в настройках отчёта поменяла бы смысл чисел.
FIELDS = {
    "начальный остаток": "opening",
    "приход": "incoming",
    "расход": "outgoing",
    "конечный остаток": "closing",
}

# Признак склада «в пути»: товар отгружен, но не принят.
TRANSIT = "в пути"

# Докуда искать шапку. Над ней стоят название отчёта, параметры и отбор —
# строк немного, но их число меняется вместе с настройками выгрузки.
SEARCH_DEPTH = 40


class ParseError(Exception):
    """Файл не похож на ведомость. Текст пригоден для показа пользователю."""


def _text(value: Any) -> str:
    return " ".join(str(value).split()).strip() if value is not None else ""


def _key(value: Any) -> str:
    """Подпись шапки к сравнимому виду: без регистра, точек и лишних пробелов."""
    return " ".join(_text(value).lower().replace(".", " ").split())


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value).replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def read(path: str) -> Ledger:
    """Читает ведомость. Значения формул берутся посчитанными."""
    book = load_workbook(path, data_only=True, read_only=False)
    try:
        return _sheet(book.worksheets[0])
    finally:
        book.close()


def _sheet(sheet: Worksheet) -> Ledger:
    fields_row, field_columns = _find_fields(sheet)
    # Шапка склада стоит выше подписей: между ними строка «Количество».
    head_row = _find_head(sheet, fields_row)
    columns = _label_columns(sheet, head_row)

    for label, needed in ((ARTICLE, "Артикул"), (NAME, "Номенклатура"),
                          (UNIT, "Ед. изм.")):
        if label not in columns:
            raise ParseError(
                f"В шапке нет колонки «{needed}» — это не похоже на ведомость "
                "по товарам на складах")

    stores, total_column = _stores(sheet, head_row, field_columns)
    if not stores:
        raise ParseError("В шапке не нашлось ни одного склада")

    ledger = Ledger(stores=stores, title=_title(sheet, head_row))
    ledger.items = _items(sheet, fields_row + 1, columns, stores, field_columns)
    if total_column:
        ledger.reported = _reported(ledger.items, total_column, field_columns,
                                    sheet, fields_row + 1, columns[UNIT])
    return ledger


def _find_fields(sheet: Worksheet) -> tuple[int, dict[int, str]]:
    """Строка с «Начальный остаток · Приход · Расход · Конечный остаток»."""
    for row in range(1, min(SEARCH_DEPTH, sheet.max_row) + 1):
        found = {cell.column: FIELDS[_key(cell.value)]
                 for cell in sheet[row] if _key(cell.value) in FIELDS}
        if len(found) >= len(FIELDS):
            return row, found
    raise ParseError(
        "Не нашлись колонки «Начальный остаток», «Приход», «Расход» и "
        "«Конечный остаток» — нужна ведомость по товарам на складах, "
        "выгруженная из 1С в Excel")


def _find_head(sheet: Worksheet, fields_row: int) -> int:
    """Строка с названиями складов — выше подписей, где стоит «Артикул»."""
    for row in range(fields_row - 1, 0, -1):
        if any(_key(cell.value) == ARTICLE for cell in sheet[row]):
            return row
    # «Артикул» не нашёлся: берём строку через одну — там, где в выгрузке 1С
    # стоят склады. Проверку колонок сделает вызывающий.
    return max(1, fields_row - 2)


def _label_columns(sheet: Worksheet, head_row: int) -> dict[str, int]:
    return {_key(cell.value): cell.column
            for cell in sheet[head_row] if _key(cell.value)}


def _stores(sheet: Worksheet, head_row: int,
            field_columns: dict[int, str]) -> tuple[list[Store], int]:
    """Склады из шапки. Итоговая колонка в список не попадает.

    Начало блока — колонка «Начальный остаток»: именно там в выгрузке стоит
    объединённая ячейка с названием склада. Если название пусто, ищем ближайшее
    слева — так бывает, когда объединение при пересохранении потерялось.
    """
    starts = sorted(column for column, field in field_columns.items()
                    if field == "opening")
    stores: list[Store] = []
    seen: dict[str, int] = {}
    total_column = 0

    for column in starts:
        name = _name_at(sheet, head_row, column)
        if not name:
            continue
        if _key(name) == TOTAL:
            total_column = column
            continue
        seen[name] = seen.get(name, 0) + 1
        # Одноимённые склады различаются номером: без него данные одного
        # затирают данные другого при любой группировке по названию.
        title = name if seen[name] == 1 else f"{name} ({seen[name]})"
        stores.append(Store(name=name, title=title, column=column,
                            transit=TRANSIT in _key(name)))

    return stores, total_column


def _name_at(sheet: Worksheet, row: int, column: int) -> str:
    """Название склада над колонкой. Ищется влево, пока не найдётся непустое."""
    for step in range(column, 0, -1):
        name = _text(sheet.cell(row, step).value)
        if name:
            return name
    return ""


def _title(sheet: Worksheet, head_row: int) -> str:
    """Название отчёта — первая непустая строка над шапкой."""
    for row in range(1, head_row):
        for cell in sheet[row]:
            if text := _text(cell.value):
                return text
    return "Ведомость по товарам на складах"


def _items(sheet: Worksheet, first_row: int, columns: dict[str, int],
           stores: list[Store], field_columns: dict[int, str]) -> list[Item]:
    article_column = columns[ARTICLE]
    name_column = columns[NAME]
    unit_column = columns[UNIT]
    items: list[Item] = []

    for row in range(first_row, sheet.max_row + 1):
        unit = _text(sheet.cell(row, unit_column).value)
        if not unit:
            # Строка группы: её числа — итоги вложенных товаров, и складывать
            # их вместе с товарами значило бы посчитать всё дважды.
            continue
        name = _text(sheet.cell(row, name_column).value)
        article = _text(sheet.cell(row, article_column).value)
        if not (name or article):
            continue

        item = Item(article=article, name=name, unit=unit)
        for store in stores:
            move = Movement()
            for offset in range(4):
                column = store.column + offset
                field = field_columns.get(column)
                if field:
                    setattr(move, field, _number(sheet.cell(row, column).value))
            if not move.empty:
                item.moves[store.title] = move
        items.append(item)
    return items


def _reported(items: list[Item], total_column: int,
              field_columns: dict[int, str], sheet: Worksheet,
              first_row: int, unit_column: int) -> Movement:
    """Сумма итоговой колонки по строкам товаров.

    Считается по тем же строкам, что и сами товары: итог по группам в файле
    продублирован, и суммировать подряд всё, что стоит в колонке «Итого»,
    значило бы получить кратно больше и решить, что разбор неверен.
    """
    result = Movement()
    for row in range(first_row, sheet.max_row + 1):
        if not _text(sheet.cell(row, unit_column).value):
            continue
        for offset in range(4):
            field = field_columns.get(total_column + offset)
            if field:
                setattr(result, field, getattr(result, field)
                        + _number(sheet.cell(row, total_column + offset).value))
    return result
