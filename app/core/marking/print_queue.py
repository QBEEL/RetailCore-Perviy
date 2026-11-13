"""Очередь оставшихся этикеток с привязкой к исходным блокам."""
from dataclasses import dataclass
from . import issue


@dataclass(frozen=True)
class Entry:
    batch: issue.Batch
    index: int

    @property
    def code(self) -> str:
        return self.batch.codes[self.index]


def pending(batches: list[issue.Batch]) -> list[Entry]:
    if len({batch.contour for batch in batches}) > 1:
        raise ValueError("Выберите блоки одного контура Честного знака.")
    result = []
    seen = set()
    for batch in batches:
        ranges = iter(batch.print_ranges())
        current = next(ranges, None)
        for index, code in enumerate(batch.codes):
            while current and index >= current[1]:
                current = next(ranges, None)
            if current and current[0] <= index < current[1]:
                continue
            if code in seen:
                raise ValueError("В выбранных блоках повторяются коды. Исключите дублирующий блок.")
            result.append(Entry(batch, index))
            seen.add(code)
    return result


def record(entries: list[Entry]) -> None:
    """Сохраняет прогресс каждого блока один раз за порцию."""
    grouped = {}
    for entry in entries:
        key = id(entry.batch)
        if key not in grouped:
            grouped[key] = (entry.batch, [])
        grouped[key][1].append(entry.index)
    for batch, indices in grouped.values():
        start = previous = indices[0]
        for index in indices[1:]:
            if index != previous + 1:
                batch.printed_ranges.append([start, previous + 1])
                start = index
            previous = index
        issue.mark_printed_range(batch, start, previous + 1)
