"""Сравнение старых цен шаблона 1С с новыми ценами поставщика."""
from __future__ import annotations

from typing import Sequence

from ..normalize import normalize_text
from .mapping import SupplierProfile
from .models import PriceCell, PriceLine, PriceStats, PriceStatus, PriceType
from .onec import OneCTemplate
from .supplier import SupplierColumn, SupplierPrice, as_price


def compare(
    lines: Sequence[PriceLine],
    template: OneCTemplate,
    supplier: SupplierPrice,
    profile: SupplierProfile,
) -> PriceStats:
    """Проставляет каждой строке новые цены и итоговый статус.

    Признак — выбранный вариант, а не прежний статус: после ручной привязки
    цена должна пересчитаться, а после снятия — исчезнуть. Строки без варианта
    остаются со старой ценой шаблона: в 1С позиция загрузится с прежней ценой,
    а не пропадёт из номенклатуры.
    """
    types = template.valid_types
    columns = _columns(types, supplier, profile)

    for line in lines:
        source = line.source
        if source is None:
            line.cells = []
            line.status = PriceStatus.REVIEW if line.alternatives else PriceStatus.NOT_FOUND
            continue

        cells: list[PriceCell] = []
        for index, price_type in enumerate(types):
            cells.append(PriceCell(
                type_index=index,
                old=_old_price(line, price_type),
                new=supplier.price_of(source, columns[index]),
            ))
        line.cells = cells
        line.status = _status(cells)
    return PriceStats.of(list(lines))


def _columns(
    types: Sequence[PriceType],
    supplier: SupplierPrice,
    profile: SupplierProfile,
) -> list[SupplierColumn | None]:
    return [supplier.column_by_title(profile.column_for(t)) for t in types]


def _old_price(line: PriceLine, price_type: PriceType) -> float | None:
    """Старая цена читается из самого шаблона — из колонки этого вида цены."""
    index = price_type.old_column - 1
    values = line.record.values
    if index < 0 or index >= len(values):
        return None
    return as_price(values[index])


def _status(cells: Sequence[PriceCell]) -> PriceStatus:
    if not any(cell.known for cell in cells):
        return PriceStatus.NO_PRICE
    return PriceStatus.CHANGED if any(cell.changed for cell in cells) else PriceStatus.UNCHANGED


def describe(price_type: PriceType, profile: SupplierProfile) -> str:
    """Строка «Закупочная ← Закупка нвоая» для подписи в интерфейсе."""
    source = profile.column_for(price_type)
    return f"{price_type.name} ← {source}" if source else f"{price_type.name} — не заполняется"


def mapped_types(template: OneCTemplate, profile: SupplierProfile) -> list[PriceType]:
    """Виды цен, для которых выбрана колонка поставщика."""
    return [t for t in template.valid_types if normalize_text(profile.column_for(t))]
