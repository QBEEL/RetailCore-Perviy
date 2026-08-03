"""Построение сводной по профилю отчёта.

Заменяет ручную цепочку «сводная таблица → удалить лишнее»: фильтр, группировка
и итоги описаны профилем, поэтому результат одинаков в июне и в декабре, у
любого менеджера и на любом наборе исходных файлов.

Порядок колонок и строк детерминирован — по алфавиту ключей. Порядок «как в
Excel вышло» воспроизвести нельзя, а поставщик сравнивает отчёты между месяцами
и вправе ожидать, что одна и та же акция стоит на том же месте.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from .models import (
    Cell,
    ColumnGroup,
    Field,
    Metric,
    ReportProfile,
    ReportRow,
    ReportTable,
    SaleRow,
    as_fields,
    as_metrics,
    period_of,
)
from .stores import StoreMap, apply_rules
from .models import StoreRule


def build(
    rows: Sequence[SaleRow],
    profile: ReportProfile,
    rules: Iterable[StoreRule] = (),
    *,
    source_files: Sequence[str] = (),
) -> tuple[ReportTable, StoreMap]:
    """Собирает готовую сводную. Вторым значением — карта переносов магазинов.

    Карта возвращается отдельно, а не прячется внутрь таблицы: интерфейс
    показывает её пользователю до выгрузки, включая обнаруженные циклы, а
    выгрузка про магазины уже ничего не знает.
    """
    moved = 0
    mapping = StoreMap()
    if profile.apply_store_rules:
        rows, mapping, moved = apply_rules(rows, rules)
    else:
        for row in rows:
            row.store = row.source_store or row.store

    # Приведение, а не `list()`: профиль мог прийти из интерфейса, где роли
    # прошли через хранилище Qt и вернулись обычными строками. Дальше по коду
    # они нужны именно членами перечисления.
    table = ReportTable(
        profile=profile,
        row_fields=as_fields(profile.rows) or [Field.ITEM],
        column_fields=as_fields(profile.columns),
        metrics=as_metrics(profile.metrics) or [Metric.QTY],
        source_rows=len(rows),
        moved_rows=moved,
        source_files=list(dict.fromkeys(source_files)),
    )

    kept = [row for row in rows if profile.filters.allows(row)]
    table.used_rows = len(kept)
    table.dropped_rows = len(rows) - len(kept)
    table.period = period_of(kept or rows)
    table.stores = sorted({row.store for row in kept if row.store})
    if not kept:
        table.totals = [Cell(metric) for metric in table.metrics]
        return table, mapping

    groups = _groups(kept, table.column_fields)
    table.groups = [ColumnGroup(keys) for keys in groups]
    index_of = {keys: position for position, keys in enumerate(groups)}

    buckets: dict[tuple[str, ...], list[float]] = {}
    width = len(groups) * len(table.metrics)
    for row in kept:
        key = _key(row, table.row_fields)
        cells = buckets.get(key)
        if cells is None:
            cells = buckets[key] = [0.0] * width
        base = index_of[_key(row, table.column_fields)] * len(table.metrics)
        for offset, metric in enumerate(table.metrics):
            cells[base + offset] += metric.of(row)

    table.rows = [
        ReportRow(keys=key, cells=[Cell(table.metrics[position % len(table.metrics)], value)
                                   for position, value in enumerate(values)])
        for key, values in sorted(buckets.items(), key=_row_order)
    ]
    table.totals = _totals(table)
    if profile.stores_sheet:
        table.store_totals = _by_store(kept, table.metrics)
    return table, mapping


def _key(row: SaleRow, fields: Sequence[Field]) -> tuple[str, ...]:
    return tuple(row.text(role) for role in fields)


def _groups(rows: Sequence[SaleRow], fields: Sequence[Field]) -> list[tuple[str, ...]]:
    """Значения разреза колонок в устойчивом порядке.

    Без полей разреза остаётся одна безымянная группа: отчёт вырождается в
    «номенклатура и итог», и это законный вариант профиля.
    """
    if not fields:
        return [()]
    return sorted({_key(row, fields) for row in rows}, key=_group_order)


def _group_order(keys: tuple[str, ...]) -> tuple:
    """Пустое значение разреза («без акции») уходит в конец."""
    return tuple((not key, _natural(key)) for key in keys)


def _row_order(item: tuple[tuple[str, ...], list[float]]) -> tuple:
    return tuple(_natural(key) for key in item[0])


def _natural(text: str) -> tuple:
    """Ключ сортировки, при котором «список_20%» идёт перед «список_100%».

    Обычная строковая сортировка ставит «100» перед «20», и колонки акций в
    отчёте выстраиваются в порядке, который поставщику ничего не говорит.
    """
    parts: list[tuple[int, float | str]] = []
    number = ""
    for char in text.lower().replace("ё", "е"):
        if char.isdigit():
            number += char
            continue
        if number:
            parts.append((0, float(number)))
            number = ""
        parts.append((1, char))
    if number:
        parts.append((0, float(number)))
    return tuple(parts)


def _totals(table: ReportTable) -> list[Cell]:
    width = len(table.groups) * len(table.metrics)
    values = [0.0] * width
    for row in table.rows:
        for position, cell in enumerate(row.cells):
            values[position] += cell.value
    return [Cell(table.metrics[position % len(table.metrics)], value)
            for position, value in enumerate(values)]


def _by_store(rows: Sequence[SaleRow], metrics: Sequence[Metric]
              ) -> list[tuple[str, list[Cell]]]:
    """Итоги по магазинам для отдельного листа.

    Считаются по магазину после переносов: лист отвечает на вопрос «сколько
    учтено за каждой точкой», а не «что откуда приехало».
    """
    buckets: dict[str, list[float]] = {}
    for row in rows:
        name = row.store or "Без магазина"
        values = buckets.get(name)
        if values is None:
            values = buckets[name] = [0.0] * len(metrics)
        for offset, metric in enumerate(metrics):
            values[offset] += metric.of(row)
    return [(name, [Cell(metrics[offset], value) for offset, value in enumerate(values)])
            for name, values in sorted(buckets.items(), key=lambda pair: _natural(pair[0]))]


def preview(table: ReportTable, limit: int = 200) -> list[list[str]]:
    """Плоское представление для таблицы на вкладке.

    Первая строка — заголовки: «Акция · Количество, шт». Полноценную двухэтажную
    шапку интерфейсу показывать незачем, а в файле она есть.
    """
    header = [role.title for role in table.row_fields]
    for group in table.groups:
        for metric in table.metrics:
            header.append(f"{group.label} · {metric.title}" if group.label != "Всего"
                          else metric.title)
    result = [header]
    for row in table.rows[:limit]:
        line = list(row.keys)
        line.extend(_show(cell) for cell in row.cells)
        result.append(line)
    return result


def _show(cell: Cell) -> str:
    if cell.empty:
        return ""
    if cell.metric.money:
        return f"{cell.value:,.2f}".replace(",", " ").replace(".", ",")
    value = round(cell.value, 3)
    return str(int(value)) if float(value).is_integer() else str(value)
