"""Узнавание поставщика по присланному файлу.

Один признак ненадёжен: имя файла бывает обезличенным («переоценка_08_2026»),
а заголовки поставщик иногда переименовывает. Поэтому признаки складываются, и
каждый объясняет себя — пользователь видит, почему приложение решило именно так,
и может выбрать другого поставщика вручную.
"""
from __future__ import annotations

import os
from typing import Sequence

from ..normalize import normalize_text
from .models import Guess, Supplier, SupplierLayout
from .store import signature_of

# Вес признака. Уверенным считается итог от 1.0 — его даёт любой из сильных
# признаков сам по себе, либо два слабых вместе.
_NAME_WEIGHT = 0.6
_ALIAS_WEIGHT = 0.8
_SIGNATURE_WEIGHT = 1.0
_SIMILAR_WEIGHT = 0.7
# Ниже этой доли общих заголовков структуры считаются разными.
_SIMILAR_THRESHOLD = 0.6


def identify(
    path: str,
    titles: Sequence[str],
    sheet_name: str,
    suppliers: Sequence[Supplier],
    layouts: Sequence[SupplierLayout],
    aliases: dict[int, list[str]] | None = None,
) -> Guess | None:
    """Чей это прайс. Возвращает лучшую догадку либо None, если совпадений нет."""
    if not suppliers:
        return None
    signature = signature_of(titles)
    stem = normalize_text(os.path.splitext(os.path.basename(path))[0])
    by_supplier = {supplier.id: supplier for supplier in suppliers}
    aliases = aliases or {}

    guesses: dict[int, Guess] = {}

    def note(supplier_id: int, weight: float, reason: str,
             layout: SupplierLayout | None = None) -> None:
        supplier = by_supplier.get(supplier_id)
        if supplier is None:
            return
        guess = guesses.setdefault(supplier_id, Guess(supplier=supplier))
        guess.score += weight
        guess.reasons.append(reason)
        # Структура запоминается от самого сильного признака, который её назвал.
        if layout is not None and (guess.layout is None or weight >= _SIGNATURE_WEIGHT):
            guess.layout = layout

    for supplier in suppliers:
        if supplier.key and supplier.key in stem:
            note(supplier.id, _NAME_WEIGHT, "имя файла")
        for key in aliases.get(supplier.id, ()):
            if key and key in stem:
                note(supplier.id, _ALIAS_WEIGHT, "дополнительное имя")
                break

    for layout in layouts:
        if signature and layout.signature == signature:
            note(layout.supplier_id, _SIGNATURE_WEIGHT, "структура прайса совпала", layout)
            continue
        overlap = similarity(titles, layout.titles)
        if overlap >= _SIMILAR_THRESHOLD:
            note(layout.supplier_id, _SIMILAR_WEIGHT * overlap,
                 f"структура похожа на {overlap * 100:.0f} %", layout)

    if not guesses:
        return None
    best = max(guesses.values(), key=lambda g: (g.score, g.supplier.id))
    if best.layout is None:
        best.layout = _preferred(best.supplier.id, layouts, sheet_name)
    return best


def similarity(titles: Sequence[str], other: Sequence[str]) -> float:
    """Доля общих заголовков — мера Жаккара по нормализованным подписям."""
    left = {normalized for t in titles if (normalized := normalize_text(t))}
    right = {normalized for t in other if (normalized := normalize_text(t))}
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _preferred(
    supplier_id: int,
    layouts: Sequence[SupplierLayout],
    sheet_name: str,
) -> SupplierLayout | None:
    """Структура поставщика, когда ни одна не совпала по заголовкам.

    Предпочитается та, что читалась с того же листа: у поставщика с несколькими
    листами это почти всегда нужная.
    """
    own = [layout for layout in layouts if layout.supplier_id == supplier_id]
    if not own:
        return None
    same_sheet = [layout for layout in own if layout.sheet_name == sheet_name]
    return (same_sheet or own)[0]


def suggest_aliases(path: str, name: str) -> list[str]:
    """Слова из имени файла, которые стоит запомнить как имена поставщика."""
    stem = os.path.splitext(os.path.basename(path))[0]
    known = normalize_text(name)
    found: list[str] = []
    for word in stem.replace("_", " ").replace("-", " ").split():
        key = normalize_text(word)
        if len(key) < 3 or any(ch.isdigit() for ch in key):
            continue
        if key in known or key in ("переоценка", "прайс", "прайслист", "price", "лист", "новые"):
            continue
        if key not in found:
            found.append(word)
    return found
