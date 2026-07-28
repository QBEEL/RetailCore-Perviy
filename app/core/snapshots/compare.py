"""Сравнение двух снимков: новые, ушедшие и изменившиеся товары."""
from __future__ import annotations

from typing import Sequence

from .models import COMPARED, ProductChange, SnapshotDiff, SnapshotProduct

# Цена в прайсах хранится по-разному (2790 и 2790.0), поэтому копейки
# сравниваются с допуском, иначе каждый второй товар выглядел бы изменившимся.
_PRICE_EPSILON = 0.005


def diff(before: Sequence[SnapshotProduct], after: Sequence[SnapshotProduct]) -> SnapshotDiff:
    """Различия между версиями. Товары сопоставляются по `SnapshotProduct.key`."""
    old = _by_key(before)
    new = _by_key(after)

    result = SnapshotDiff()
    for key, product in new.items():
        previous = old.get(key)
        if previous is None:
            result.added.append(product)
        elif fields := changed_fields(previous, product):
            result.changed.append(ProductChange(previous, product, fields))
    result.removed.extend(product for key, product in old.items() if key not in new)
    return result


def changed_fields(before: SnapshotProduct, after: SnapshotProduct) -> list[str]:
    fields = [name for name in COMPARED
              if getattr(before, name, "") != getattr(after, name, "")]
    if _price_differs(before.price, after.price):
        fields.append("price")
    return fields


def _price_differs(before: float | None, after: float | None) -> bool:
    if before is None or after is None:
        return before is not after
    return abs(before - after) > _PRICE_EPSILON


def _by_key(products: Sequence[SnapshotProduct]) -> dict[str, SnapshotProduct]:
    """Первое вхождение ключа выигрывает: дубли в прайсе не должны множить различия."""
    result: dict[str, SnapshotProduct] = {}
    for product in products:
        if (key := product.key) and key not in result:
            result[key] = product
    return result
