"""Несколько выгрузок — одна ведомость.

Подразделения присылают отчёт по-разному: кто одним файлом на все склады, кто
по файлу на магазин. Каждый файл читается своим видом разбора, а сводятся уже
разобранные ведомости — поэтому файлы разного вида складываются вместе.

Склады из разных файлов не сливаются, даже если называются одинаково: один и
тот же склад в двух файлах — это, скорее всего, файл, выбранный дважды, и
сложить его значило бы удвоить расход. Такие склады разводятся номером, как и
одноимённые склады внутри одного файла, и вкладка о них предупреждает.

Товары сводятся по коду 1С, а где кода нет — по артикулу, названию и единице.
"""
from __future__ import annotations

import os

from .models import FIELDS, Item, Ledger, Movement, Store
from .parse import ParseError, item_key, read

DEFAULT_TITLE = "Ведомость по товарам на складах"


def read_many(paths: list[str]) -> Ledger:
    """Читает файлы и сводит их. Ошибка называет файл, на котором случилась."""
    ledgers = []
    for path in paths:
        try:
            ledgers.append(read(path))
        except ParseError as error:
            raise ParseError(f"{os.path.basename(path)}: {error}") from error
    return merge(ledgers)


def merge(ledgers: list[Ledger]) -> Ledger:
    if not ledgers:
        raise ParseError("Не выбрано ни одного файла")
    if len(ledgers) == 1:
        return ledgers[0]

    titles = {ledger.title for ledger in ledgers}
    fields = frozenset.intersection(*(ledger.fields for ledger in ledgers))
    result = Ledger(
        title=titles.pop() if len(titles) == 1 else DEFAULT_TITLE,
        layout=", ".join(dict.fromkeys(ledger.layout for ledger in ledgers)),
        fields=fields)

    used: dict[str, int] = {}
    by_key: dict[tuple, Item] = {}
    by_name: dict[tuple, Item] = {}
    for ledger in ledgers:
        source = ", ".join(ledger.sources) or "файл"
        result.sources.extend(ledger.sources)
        result.warnings.extend(f"{source}: {note}" for note in ledger.warnings)
        if not ledger.balanced:
            result.warnings.append(
                f"{source}: сумма по складам не сошлась с «Итого» в файле")

        renamed = {store.title: _place(result, store, used)
                   for store in ledger.stores}
        for item in ledger.items:
            known = _find(item, by_key, by_name, result.items)
            for title, move in item.moves.items():
                known.add(renamed[title], move)

    # Сверка сводной — по сумме итогов файлов, если итог был в каждом.
    if all(ledger.reported is not None for ledger in ledgers):
        result.reported = Movement()
        for ledger in ledgers:
            for name in FIELDS:
                setattr(result.reported, name, getattr(result.reported, name)
                        + getattr(ledger.reported, name))
    return result


def _place(result: Ledger, store: Store, used: dict[str, int]) -> str:
    """Добавляет склад в сводную, разводя номером совпавшие названия."""
    used[store.name] = used.get(store.name, 0) + 1
    title = store.name if used[store.name] == 1 else f"{store.name} ({used[store.name]})"
    result.stores.append(Store(name=store.name, title=title,
                               column=store.column, transit=store.transit))
    return title


def _find(item: Item, by_key: dict[tuple, Item], by_name: dict[tuple, Item],
          items: list[Item]) -> Item:
    """Товар в сводной. Ищется по коду, затем по артикулу и названию.

    Второй поиск нужен, когда в одном файле код есть, а в другом колонку кода
    не включили: без него один товар встал бы двумя строками.
    """
    name_key = item_key(Item(article=item.article, name=item.name, unit=item.unit))
    known = by_key.get(item_key(item)) or by_name.get(name_key)
    if known is None:
        known = Item(article=item.article, name=item.name, unit=item.unit,
                     code=item.code)
        items.append(known)
    known.code = known.code or item.code
    known.article = known.article or item.article
    by_key.setdefault(item_key(known), known)
    by_name.setdefault(name_key, known)
    return known
