"""Запись новых цен в шаблон 1С.

Итоговый файл — это исходный шаблон, в котором изменены только ячейки «Цена».
Всё остальное остаётся нетронутым: оба листа, объединённая шапка, порядок и
названия колонок, ширины, скрытые служебные колонки, форматы чисел, стили и
формулы процента.

Колонки «Изменение» и «%» специально не заполняются: в шаблоне это формулы
(``=IF(RC6<>0,ROUND((RC9-RC6)/RC6*100,2),0)`` в стиле R1C1), и они пересчитаются
сами. Запись готового значения поверх формулы сломала бы шаблон для следующей
выгрузки.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Sequence

from ..workbook import open_workbook
from .models import PriceLine, PriceStatus, PriceType
from .onec import OneCTemplate

ProgressCallback = Callable[[int, int], None]

# Формат по умолчанию для ячейки цены, если в шаблоне он не задан.
_PRICE_FORMAT = "#,##0.00"


@dataclass(slots=True)
class ExportReport:
    """Что получилось записать."""

    path: str
    rows: int = 0
    cells: int = 0
    removed: int = 0

    @property
    def file_name(self) -> str:
        return os.path.basename(self.path)


def export(
    template: OneCTemplate,
    lines: Sequence[PriceLine],
    destination: str,
    *,
    skip_unchanged: bool = False,
    progress: ProgressCallback | None = None,
) -> ExportReport:
    """Сохраняет копию шаблона с новыми ценами.

    `skip_unchanged` убирает из файла строки, для которых новая цена не
    записывается: не найденные, требующие сопоставления и те, чья цена не
    изменилась. Строки удаляются снизу вверх, чтобы номера остальных не
    поехали, а сквозная нумерация «№» пересчитывается заново.
    """
    if not destination:
        raise ValueError("Не указан файл для сохранения")

    types = template.valid_types
    book = open_workbook(template.path, data_only=False)
    try:
        sheet = _sheet(book, template.sheet_name)
        report = ExportReport(path=destination)
        total = len(lines)
        for index, line in enumerate(lines):
            written = _write_line(sheet, line, types)
            if written:
                report.rows += 1
                report.cells += written
            if progress and (index % 200 == 0 or index == total - 1):
                progress(index + 1, total)

        if skip_unchanged:
            report.removed = _drop_rows(
                sheet, [line.row for line in lines if not line.writable])
            _renumber(sheet, template)

        _ensure_recalculation(book)
        book.save(destination)
        return report
    finally:
        book.close()


def _sheet(book, name: str):
    if name in book.sheetnames:
        return book[name]
    raise ValueError(f"В шаблоне нет листа «{name}» — файл изменился после загрузки")


def _write_line(sheet, line: PriceLine, types: Sequence[PriceType]) -> int:
    """Пишет новые цены одной строки. Возвращает число изменённых ячеек."""
    if line.status is not PriceStatus.CHANGED:
        return 0
    written = 0
    for cell_data in line.cells:
        if not cell_data.changed or cell_data.type_index >= len(types):
            continue
        price_type = types[cell_data.type_index]
        if price_type.price_column <= 0:
            continue
        cell = sheet.cell(row=line.row, column=price_type.price_column)
        cell.value = _rounded(cell_data.new)
        if cell.number_format in (None, "", "General"):
            cell.number_format = _source_format(sheet, line.row, price_type)
        written += 1
    return written


def _source_format(sheet, row: int, price_type: PriceType) -> str:
    """Формат берётся у «Старой цены» той же строки — он там уже правильный."""
    if price_type.old_column > 0:
        existing = sheet.cell(row=row, column=price_type.old_column).number_format
        if existing and existing != "General":
            return existing
    return _PRICE_FORMAT


def _rounded(value: float | None) -> float | int | None:
    """Целая цена записывается целым числом — так её и присылает поставщик."""
    if value is None:
        return None
    value = round(float(value), 2)
    return int(value) if value.is_integer() else value


def _drop_rows(sheet, rows: Sequence[int]) -> int:
    """Удаляет строки снизу вверх, соседние — одним блоком.

    Формулы шаблона записаны в стиле R1C1 и ссылаются на соседние ячейки той же
    строки, поэтому сдвиг строк их не портит.
    """
    ordered = sorted({row for row in rows if row > 0}, reverse=True)
    if not ordered:
        return 0
    removed = 0
    block_end = block_start = ordered[0]
    for row in ordered[1:]:
        if row == block_start - 1:
            block_start = row
            continue
        sheet.delete_rows(block_start, block_end - block_start + 1)
        removed += block_end - block_start + 1
        block_end = block_start = row
    sheet.delete_rows(block_start, block_end - block_start + 1)
    return removed + block_end - block_start + 1


def _renumber(sheet, template: OneCTemplate) -> None:
    """Восстанавливает сквозную нумерацию «№» после удаления строк."""
    column = _number_column(template)
    if not column or not template.article_column:
        return
    counter = 0
    for row in range(template.header_row + 1, sheet.max_row + 1):
        if sheet.cell(row=row, column=template.article_column).value is None:
            continue
        counter += 1
        sheet.cell(row=row, column=column).value = counter


def _number_column(template: OneCTemplate) -> int:
    """Колонка «№» — первая, если она не занята артикулом или названием."""
    taken = {template.article_column, template.name_column,
             template.sku_column, template.ean_column}
    title = template.titles[0].strip() if template.titles else ""
    return 1 if 1 not in taken and title in ("№", "N", "#", "п/п", "№ п/п") else 0


def _ensure_recalculation(book) -> None:
    """Просит Excel пересчитать формулы при открытии — иначе «%» останется старым."""
    if book.calculation is not None:
        book.calculation.fullCalcOnLoad = True
