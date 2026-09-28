"""Несколько выгрузок — один отчёт.

Выгрузку по чекам удобно делать помесячно: большой период 1С собирает долго.
Файлы сводятся в один отчёт, период — от первого дня до последнего.

Чек, попавший в два файла, берётся один раз. Так бывает, когда периоды
выгрузок перекрываются или один файл выбран дважды: сложить такой чек дважды
значило бы удвоить выручку дня, и заметить это по итогам было бы нечем.
"""
from __future__ import annotations

from .models import Receipts
from .parse import read


def read_many(paths: list[str]) -> Receipts:
    return merge([read(path) for path in paths])


def merge(parts: list[Receipts]) -> Receipts:
    if len(parts) == 1:
        return parts[0]
    result = Receipts()
    # Владелец чека — номер файла в списке, а не имя: один и тот же файл,
    # выбранный дважды, имеет одно имя, и по имени задвоение не отличить.
    seen: dict[str, int] = {}
    skipped: dict[str, int] = {}
    for number, part in enumerate(parts):
        source = part.sources[0] if part.sources else f"файл {number + 1}"
        result.sources.extend(part.sources)
        for line in part.lines:
            owner = seen.setdefault(line.document, number)
            if owner != number:
                skipped[source] = skipped.get(source, 0) + 1
                continue
            result.lines.append(line)
        result.warnings.extend(f"{source}: {text}" for text in part.warnings)
        result.missing = sorted(set(result.missing) | set(part.missing))
        result.units = result.units or part.units
        result.selection = result.selection or part.selection
        if part.balanced is False:
            result.balanced = False
        elif result.balanced is None:
            result.balanced = part.balanced

    starts = [part.start for part in parts if part.start]
    ends = [part.end for part in parts if part.end]
    result.start = min(starts) if starts else None
    result.end = max(ends) if ends else None
    for source, count in skipped.items():
        result.warnings.append(
            f"{source}: {count} строк из чеков, которые уже есть в другом файле, "
            "не учтены — периоды выгрузок перекрываются")
    return result
