"""Соответствие колонок поставщика колонкам шаблона 1С.

Модуль не знает ни одного конкретного поставщика. Соответствие подбирается
эвристикой по заголовкам, правится пользователем и сохраняется в профиль —
так нового поставщика подключают без правки кода.

Профиль хранит **заголовки**, а не номера колонок: в следующем файле того же
поставщика колонка может сдвинуться, а называться будет так же.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..article import DEFAULT_MODIFIER_SEPARATORS, DEFAULT_SEPARATORS
from ..models import FieldRole
from ..normalize import normalize_text
from .models import PriceType
from .supplier import SupplierColumn, SupplierPrice

# Слова, по которым распознаётся смысл колонки и вида цены.
_NEW_WORDS = ("нов", "нвоа", "new", "будущ", "станет", "с 0", "с 1", "с 2", "с 3")
_OLD_WORDS = ("текущ", "стар", "прежн", "old", "current", "было", "действующ")
_PURCHASE_WORDS = ("закуп", "оптов", "purchase", "cost", "себестоим", "приход")
_RETAIL_WORDS = ("ррц", "розн", "retail", "rrp", "msrp", "прайс", "продаж")
_PROMO_WORDS = ("промо", "акци", "скидк", "promo", "sale")

_KIND_PURCHASE = "purchase"
_KIND_RETAIL = "retail"


@dataclass(slots=True)
class SupplierProfile:
    """Сохранённые настройки одного поставщика."""

    name: str = ""
    sheet: str = ""
    # Имя вида цены 1С -> заголовок колонки поставщика.
    price_map: dict[str, str] = field(default_factory=dict)
    # Роль -> заголовок колонки поставщика; пусто — определять автоматически.
    role_map: dict[str, str] = field(default_factory=dict)
    separators: str = DEFAULT_SEPARATORS
    modifier_separators: str = DEFAULT_MODIFIER_SEPARATORS

    def column_for(self, price_type: PriceType) -> str:
        return self.price_map.get(price_type.name, "")

    def overrides(self, supplier: SupplierPrice) -> dict[int, FieldRole]:
        """Ручные роли колонок, пересчитанные в номера для текущего файла."""
        by_title = {normalize_text(c.title): c.index for c in supplier.columns}
        result: dict[int, FieldRole] = {}
        for role_name, title in self.role_map.items():
            index = by_title.get(normalize_text(title))
            try:
                role = FieldRole(role_name)
            except ValueError:
                continue
            if index is not None:
                result[index] = role
        return result

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sheet": self.sheet,
            "price_map": dict(self.price_map),
            "role_map": dict(self.role_map),
            "separators": self.separators,
            "modifier_separators": self.modifier_separators,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SupplierProfile":
        return cls(
            name=str(data.get("name") or ""),
            sheet=str(data.get("sheet") or ""),
            price_map=_string_map(data.get("price_map")),
            role_map=_string_map(data.get("role_map")),
            separators=str(data.get("separators") or DEFAULT_SEPARATORS),
            modifier_separators=str(data.get("modifier_separators", DEFAULT_MODIFIER_SEPARATORS)),
        )


class SupplierProfiles:
    """Список профилей с подбором по файлу поставщика."""

    def __init__(self, items: Iterable[SupplierProfile] = ()) -> None:
        self.items: list[SupplierProfile] = [p for p in items if p.name]

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    def find(self, name: str) -> SupplierProfile | None:
        wanted = normalize_text(name)
        return next((p for p in self.items if normalize_text(p.name) == wanted), None)

    def for_file(self, path: str) -> SupplierProfile | None:
        """Профиль по имени файла: «Переоценка Zielinski.xlsx» → «Zielinski»."""
        if not path:
            return None
        stem = normalize_text(os.path.splitext(os.path.basename(path))[0])
        if not stem:
            return None
        best: SupplierProfile | None = None
        for profile in self.items:
            key = normalize_text(profile.name)
            if key and key in stem and (best is None or len(key) > len(normalize_text(best.name))):
                best = profile
        return best

    def remember(self, profile: SupplierProfile) -> None:
        if not profile.name:
            return
        self.items = [p for p in self.items if normalize_text(p.name) != normalize_text(profile.name)]
        self.items.append(profile)

    def forget(self, name: str) -> None:
        wanted = normalize_text(name)
        self.items = [p for p in self.items if normalize_text(p.name) != wanted]


def suggest_profile_name(path: str) -> str:
    """Имя профиля по умолчанию — файл без служебных слов и дат."""
    stem = os.path.splitext(os.path.basename(path))[0]
    words = [
        word for word in stem.replace("_", " ").split()
        if not any(ch.isdigit() for ch in word)
        and normalize_text(word) not in ("переоценка", "прайс", "прайслист", "price", "лист")
    ]
    return " ".join(words) or stem


def suggest_price_map(
    types: Sequence[PriceType],
    columns: Sequence[SupplierColumn],
) -> dict[str, str]:
    """Подбирает колонку с новой ценой для каждого вида цены 1С.

    Правило простое и переносимое между поставщиками: сначала колонки того же
    назначения, что и вид цены (закупочные к закупочным, розничные к
    розничным), внутри группы — та, что описывает новую цену. Слово «новая»
    в заголовке есть не всегда — в разборе примера оно было с опечаткой, —
    поэтому «не старая» тоже считается новой.
    """
    result: dict[str, str] = {}
    for price_type in types:
        kind = _kind(price_type.name)
        best, best_score = "", 0.0
        for column in columns:
            score = _score(column, kind)
            if score > best_score:
                best, best_score = column.title, score
        if best:
            result[price_type.name] = best
    return result


def _score(column: SupplierColumn, kind: str) -> float:
    title = normalize_text(column.title)
    if any(word in title for word in _OLD_WORDS):
        # Старая цена в шаблоне уже есть — записывать её обратно бессмысленно.
        return 0.0
    score = 1.0 if column.priced else 0.4
    column_kind = _kind(column.title)
    if kind and column_kind == kind:
        score += 4.0
    elif kind and column_kind:
        return 0.0  # закупочную цену нельзя записать в розничный вид цены
    if any(word in title for word in _NEW_WORDS):
        score += 3.0
    if any(word in title for word in _PROMO_WORDS):
        # Акционная цена временная: подставляется только по явному выбору.
        score -= 2.5
    return max(score, 0.0)


def _kind(title: str) -> str:
    normalized = normalize_text(title)
    if any(word in normalized for word in _PURCHASE_WORDS):
        return _KIND_PURCHASE
    if any(word in normalized for word in _RETAIL_WORDS):
        return _KIND_RETAIL
    return ""


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if k and isinstance(v, str)}
