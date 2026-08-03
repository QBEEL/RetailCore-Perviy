"""Правила «магазин-источник → магазин-приёмник».

Часть продаж одного магазина учитывается за другим: точка открылась на месте
прежней, работает под её вывеской или разделяет с ней ассортимент. Раньше это
поправлялось руками в готовой сводной — раз в месяц и по памяти.

Правила общие для всех менеджеров и применяются транзитивно: если «Артем ПП»
уезжает в «Артем», а «Артем» — в «Владивосток», продажи первого доедут до
последнего за один проход. Цепочка разворачивается заранее, поэтому стоимость
применения не зависит от её длины.

Цикл (A→B→A) — не ошибка данных, а ошибка настройки, и молча выбирать «первое
правило побеждает» здесь нельзя: цифры разъедутся, а объяснения не будет.
Цикл обнаруживается при сборке карты и возвращается вызывающему целиком.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .models import SaleRow, StoreRule


def normalize(name: str) -> str:
    """Ключ сравнения названий магазинов.

    Регистр и лишние пробелы в выгрузках гуляют («Сахалин ПП» и «Сахалин  ПП»),
    а буква «ё» пишется через раз. Ключ нужен только для поиска правила —
    в отчёт всегда идёт название так, как его написал пользователь в правиле.
    """
    return " ".join(str(name or "").split()).lower().replace("ё", "е")


@dataclass(slots=True)
class StoreMap:
    """Развёрнутая карта переносов: ключ источника → итоговое название."""

    targets: dict[str, str] = field(default_factory=dict)
    cycles: list[list[str]] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.cycles

    def resolve(self, store: str) -> str:
        return self.targets.get(normalize(store), store)

    def moves(self, store: str) -> bool:
        target = self.targets.get(normalize(store))
        return bool(target) and normalize(target) != normalize(store)


def build_map(rules: Iterable[StoreRule]) -> StoreMap:
    """Сворачивает цепочки правил в прямые переносы «источник → конец цепи».

    Отключённые и незаполненные правила пропускаются. Если один источник указан
    в нескольких правилах, побеждает последнее — список приходит от сервера
    упорядоченным по времени, и «последнее слово» здесь совпадает с ожиданием
    того, кто правило только что поправил.
    """
    direct: dict[str, str] = {}
    for rule in rules:
        if not rule.enabled or not rule.valid:
            continue
        source, target = normalize(rule.source), rule.target.strip()
        if not source or normalize(target) == source:
            # Правило «магазин сам в себя» ничего не делает и только путает.
            continue
        direct[source] = target

    result = StoreMap()
    seen_cycles: set[frozenset[str]] = set()
    for source in direct:
        chain: list[str] = [source]
        visited = {source}
        current = source
        while (next_name := direct.get(current)) is not None:
            key = normalize(next_name)
            if key in visited:
                # Цикл: запоминаем его один раз и оставляем магазины на месте.
                # Полпереноса хуже, чем ни одного: часть продаж уехала бы, а
                # часть осталась, и итог не сошёлся бы ни с одной стороной.
                loop = chain[chain.index(_first(chain, key)):] + [next_name]
                if (mark := frozenset(normalize(n) for n in loop)) not in seen_cycles:
                    seen_cycles.add(mark)
                    result.cycles.append(loop)
                break
            visited.add(key)
            chain.append(next_name)
            current = key
        else:
            result.targets[source] = chain[-1]
    if result.cycles:
        # Карта с циклом не применяется вовсе — иначе часть отчёта посчиталась
        # бы по новым правилам, а часть по старым.
        result.targets.clear()
    return result


def _first(chain: Sequence[str], key: str) -> str:
    for name in chain:
        if normalize(name) == key:
            return name
    return chain[0]


def apply_rules(rows: Sequence[SaleRow], rules: Iterable[StoreRule]
                ) -> tuple[list[SaleRow], StoreMap, int]:
    """Переносит продажи по правилам. Возвращает строки, карту и число переносов.

    Строки не копируются: `SaleRow` создаётся разбором исходника и дальше никем
    не переиспользуется, а копия сотен тысяч строк на каждый отчёт — это память
    и время на ровном месте. Исходное название сохраняется в `source_store`,
    поэтому повторный вызов с другими правилами даёт тот же результат.
    """
    mapping = build_map(rules)
    if not mapping.valid:
        return list(rows), mapping, 0
    moved = 0
    for row in rows:
        origin = row.source_store or row.store
        row.source_store = origin
        target = mapping.resolve(origin)
        if normalize(target) != normalize(origin):
            row.store = target
            moved += 1
        else:
            row.store = origin
    return list(rows), mapping, moved


def unknown_sources(rules: Iterable[StoreRule], stores: Iterable[str]) -> list[str]:
    """Источники из правил, которых нет в данных.

    Обычно это опечатка или переименованный магазин: правило есть, а работать
    ему не на чем, и молчать об этом нельзя — менеджер ждёт, что продажи уехали.
    """
    known = {normalize(name) for name in stores}
    missing = []
    for rule in rules:
        if rule.enabled and rule.valid and normalize(rule.source) not in known:
            missing.append(rule.source)
    return sorted(set(missing))


def describe(mapping: StoreMap) -> list[str]:
    """Человеческое описание карты — для подсказки на вкладке."""
    return [f"{source} → {target}"
            for source, target in sorted(mapping.targets.items())]
