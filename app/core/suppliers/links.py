"""Применение сохранённых привязок к строкам переоценки.

Привязка — осознанное решение пользователя, поэтому она проверяется раньше
автоматического подбора и перекрывает его. Но молчаливой она быть не должна:
если поставщик переиспользовал артикул под другой товар, сохранённая привязка
поставила бы неверную цену — ровно тот класс ошибок, от которого весь модуль и
защищает. Поэтому расхождение названий отмечается, а исчезнувший из прайса
артикул возвращает строку автоматическому подбору.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from rapidfuzz import fuzz

from ..models import Candidate, FieldRole, Record
from ..normalize import code_key, comparable, digits_only
from ..pricing.models import PriceLine
from ..pricing.onec import OneCTemplate
from .models import LinkKey, SupplierLink

STAGE_LINK = "Привязка"
_LINK_SCORE = 100.0

# Ниже этого сходства названий привязка помечается как требующая внимания.
NAME_DRIFT_THRESHOLD = 55.0


def keys_for(template: OneCTemplate, lines: Sequence[PriceLine]) -> list[LinkKey]:
    """Ключи строк шаблона: пара идентификаторов 1С, иначе артикул с объёмом."""
    return [
        LinkKey.of(
            nomenclature=template.value_at(line.record, template.nomenclature_column),
            characteristic=template.value_at(line.record, template.characteristic_column),
            article=line.article,
            volume=str(line.quantity) if line.quantity else "",
            name=line.name,
        )
        for line in lines
    ]


class LinkBook:
    """Привязки одного поставщика с поиском по ключам строки 1С."""

    def __init__(self, items: Iterable[SupplierLink] = ()) -> None:
        self.items: list[SupplierLink] = [link for link in items if link.key.identity]
        self._by_key: dict[str, SupplierLink] = {}
        for link in self.items:
            for key in link.key.lookup():
                self._by_key.setdefault(key, link)

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    def find(self, key: LinkKey) -> SupplierLink | None:
        """Привязка этой строки — и только этой.

        Слабые ключи (артикул с объёмом, название) существуют для шаблонов без
        идентификаторов 1С. Когда идентификаторы есть у обеих сторон, совпасть
        они обязаны именно по ним: одна ячейка артикула стоит в нескольких
        строках, и по артикулу привязка соседней строки применилась бы сюда,
        молча подставив цену другого товара.
        """
        for candidate in key.lookup():
            link = self._by_key.get(candidate)
            if link is None:
                continue
            if key.strong and link.key.strong and not candidate.startswith("g:"):
                continue
            return link
        return None


class SupplierIndex:
    """Строки прайса по артикулу, коду и штрихкоду — чтобы найти цель привязки."""

    def __init__(self, records: Sequence[Record]) -> None:
        self._by_article: dict[str, Record] = {}
        self._by_sku: dict[str, Record] = {}
        self._by_ean: dict[str, Record] = {}
        for record in records:
            if key := code_key(record.by_role.get(FieldRole.ARTICLE)):
                self._by_article.setdefault(key, record)
            if key := code_key(record.by_role.get(FieldRole.SKU)):
                self._by_sku.setdefault(key, record)
            if key := digits_only(record.by_role.get(FieldRole.EAN)):
                self._by_ean.setdefault(key, record)

    def locate(self, link: SupplierLink) -> Record | None:
        """Товар, на который указывает привязка. Артикул важнее прочих ключей."""
        if record := self._by_article.get(code_key(link.supplier_article)):
            return record
        if record := self._by_sku.get(code_key(link.supplier_sku)):
            return record
        return self._by_ean.get(digits_only(link.supplier_ean))


def apply_links(
    lines: Sequence[PriceLine],
    keys: Sequence[LinkKey],
    book: LinkBook,
    records: Sequence[Record],
) -> int:
    """Проставляет сохранённые привязки. Возвращает число применённых."""
    if not book:
        return 0
    index = SupplierIndex(records)
    applied = 0
    for line, key in zip(lines, keys):
        if line.manual or not key:
            continue
        link = book.find(key)
        if link is None:
            continue
        record = index.locate(link)
        if record is None:
            # Товар пропал из прайса: пусть строку разбирает обычный подбор,
            # иначе пользователь увидел бы привязку, за которой ничего нет.
            continue
        candidate = Candidate(record, _LINK_SCORE, STAGE_LINK)
        line.assign(candidate, manual=False)
        line.linked = True
        line.link_warning = _drifted(line, record)
        applied += 1
    return applied


def _drifted(line: PriceLine, record: Record) -> bool:
    """Не подменился ли товар за артикулом с прошлого раза."""
    left, right = comparable(line.name), record.match_key
    if not left or not right:
        return False
    return fuzz.token_set_ratio(left, right) < NAME_DRIFT_THRESHOLD


def link_from(line: PriceLine, key: LinkKey, supplier_id: int) -> SupplierLink:
    """Готовит привязку к сохранению по текущему выбору пользователя."""
    source = line.source
    if source is None:
        raise ValueError("Нечего запоминать: вариант не выбран")
    return SupplierLink(
        supplier_id=supplier_id,
        key=key,
        onec_article=line.article,
        onec_name=line.name,
        supplier_article=source.text(FieldRole.ARTICLE),
        supplier_sku=source.text(FieldRole.SKU),
        supplier_ean=source.text(FieldRole.EAN),
        supplier_name=source.text(FieldRole.NAME) or source.label,
    )
