"""Сопоставление строк цели с каталогом: этапы по убыванию надёжности."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from rapidfuzz import fuzz, process

from . import article
from .models import Candidate, FieldRole, MatchResult, MatchStatus, Quantity, Record, Sheet
from .normalize import DEFAULT_KEY_OPTIONS, KeyOptions, code_key, digits_only

ProgressCallback = Callable[[int, int], None]
# Запись каталога вместе с его порядковым номером — номер задаёт приоритет.
_Ref = tuple[Record, int]

STAGE_ARTICLE = "Артикул"
STAGE_ARTICLE_BASE = "Артикул (база)"
STAGE_EAN = "EAN"
STAGE_NAME_VOLUME = "Название + объём"
STAGE_NAME = "Название"
STAGE_FUZZY = "Нечёткое совпадение"

_IDENTIFIER_SCORE = 100.0
# Совпадение по базовому артикулу слабее точного: модификацию ещё предстоит
# подтвердить объёмом или названием.
_BASE_SCORE = 94.0
_NAME_VOLUME_SCORE = 96.0
_NAME_ONLY_SCORE = 88.0


@dataclass(slots=True)
class MatchConfig:
    """Пороги сопоставления. Объём — жёсткое условие, а не подсказка.

    Значения по умолчанию повторяют поведение вкладки «Сопоставление»:
    штрафы выключены, разбор модификаций артикула выключен. Переоценка
    включает их явно — там ячейка артикула 1С перечисляет варианты одной
    номенклатуры, и выбрать нужный можно только по близости объёма.
    """

    volume_tolerance: float = 0.05
    enforce_volume: bool = True
    fuzzy_threshold: float = 72.0
    auto_accept: float = 90.0
    ambiguity_delta: float = 2.0
    max_alternatives: int = 5

    # Какие этапы участвуют в подборе.
    use_article: bool = True
    use_sku: bool = True
    use_ean: bool = True
    use_name: bool = True
    use_fuzzy: bool = True

    # Разбор артикула: перечисление вариантов и «база + модификация».
    separators: str = article.DEFAULT_SEPARATORS
    modifier_separators: str = ""
    key_options: KeyOptions = field(default_factory=lambda: DEFAULT_KEY_OPTIONS)

    # Уточнение оценки внутри этапа: чем дальше объём и название кандидата от
    # искомых, тем ниже его оценка. Ноль — прежнее поведение без уточнения.
    volume_penalty: float = 0.0
    name_penalty: float = 0.0


class Matcher:
    """Индексирует каталоги один раз и сопоставляет с ними строки цели.

    Каталогов может быть несколько: этап проходит по всем сразу, а при равной
    оценке предпочитается тот, что стоит выше в списке.
    """

    def __init__(self, sources: Sheet | Sequence[Sheet], config: MatchConfig | None = None) -> None:
        self.sources: list[Sheet] = [sources] if isinstance(sources, Sheet) else list(sources)
        self.config = config or MatchConfig()
        self._origins = [os.path.basename(sheet.path) for sheet in self.sources]
        self._by_code: dict[str, list[_Ref]] = {}
        self._by_base: dict[str, list[_Ref]] = {}
        self._by_ean: dict[str, list[_Ref]] = {}
        self._by_name: dict[str, list[_Ref]] = {}
        self._names: list[str] = []
        self._name_refs: list[_Ref] = []
        self._build_indexes()

    def _variants(self, value: object) -> list[article.Article]:
        return article.article_variants(
            value, self.config.separators, self.config.modifier_separators,
            self.config.key_options)

    def _code(self, value: object) -> str:
        return code_key(value, self.config.key_options)

    @property
    def source(self) -> Sheet | None:
        """Основной каталог — первый в списке."""
        return self.sources[0] if self.sources else None

    def _build_indexes(self) -> None:
        for catalog, sheet in enumerate(self.sources):
            for record in sheet.records:
                ref = (record, catalog)
                for role in (FieldRole.ARTICLE, FieldRole.SKU):
                    for variant in self._variants(record.by_role.get(role)):
                        self._by_code.setdefault(variant.key, []).append(ref)
                        # По базе ищут только когда точного совпадения нет,
                        # поэтому индекс наполняется всегда — лишним он не бывает.
                        if variant.base_key:
                            self._by_base.setdefault(variant.base_key, []).append(ref)
                if key := digits_only(record.by_role.get(FieldRole.EAN)):
                    self._by_ean.setdefault(key, []).append(ref)
                if name := record.match_key:
                    self._by_name.setdefault(name, []).append(ref)
                    self._names.append(name)
                    self._name_refs.append(ref)

    def _candidate(self, ref: _Ref, score: float, stage: str) -> Candidate:
        record, catalog = ref
        return Candidate(record, score, stage, origin=self._origins[catalog], catalog=catalog)

    def match_all(
        self,
        targets: Sequence[Record],
        progress: ProgressCallback | None = None,
    ) -> list[MatchResult]:
        results: list[MatchResult] = []
        total = len(targets)
        for index, target in enumerate(targets):
            results.append(self.match(target))
            if progress and (index % 25 == 0 or index == total - 1):
                progress(index + 1, total)
        return results

    def match(self, target: Record) -> MatchResult:
        result = MatchResult(target=target)
        # Совпадение по идентификатору не отбрасывается расхождением объёма:
        # артикул однозначен, а расхождение — повод для проверки, не для отказа.
        stages = (
            (self._by_identifier(target), False),
            (self._by_name_stage(target), True),
            (self._by_fuzzy(target), True),
        )
        for candidates, enforce in stages:
            if not candidates:
                continue
            accepted, rejected = self._apply_volume_gate(target, candidates, enforce)
            if accepted:
                return self._finalize(result, accepted, rejected)
            # Все кандидаты отклонены по объёму: показываем их как варианты для проверки.
            result.alternatives = rejected[: self.config.max_alternatives]
            result.status = MatchStatus.REVIEW
        return result

    def _by_identifier(self, target: Record) -> list[Candidate]:
        """Артикул и EAN — самые надёжные ключи, проверяются первыми."""
        candidates: list[Candidate] = []
        seen: set[int] = set()

        def take(ref: _Ref, score: float, stage: str) -> None:
            if id(ref[0]) not in seen:
                seen.add(id(ref[0]))
                candidates.append(self._candidate(ref, score, stage))

        roles = []
        if self.config.use_article:
            roles.append(FieldRole.ARTICLE)
        if self.config.use_sku:
            roles.append(FieldRole.SKU)

        bases: list[str] = []
        for role in roles:
            for variant in self._variants(target.by_role.get(role)):
                for ref in self._by_code.get(variant.key, ()):
                    take(ref, _IDENTIFIER_SCORE, STAGE_ARTICLE)
                if variant.base_key and variant.base_key != variant.key:
                    bases.append(variant.base_key)
        if self.config.use_ean:
            for ref in self._by_ean.get(digits_only(target.by_role.get(FieldRole.EAN)), ()):
                take(ref, _IDENTIFIER_SCORE, STAGE_EAN)
        # База проверяется, только если точного артикула нет ни у одной записи:
        # «ABC123/50» сначала должен найти «ABC123/50», и лишь потом — семейство
        # «ABC123», из которого нужный вариант выберет объём.
        if not candidates:
            for base in bases:
                for ref in self._by_base.get(base, ()):
                    take(ref, _BASE_SCORE, STAGE_ARTICLE_BASE)
        return candidates

    def _by_name_stage(self, target: Record) -> list[Candidate]:
        name = target.match_key
        if not name or not self.config.use_name:
            return []
        stage = STAGE_NAME_VOLUME if target.quantity else STAGE_NAME
        score = _NAME_VOLUME_SCORE if target.quantity else _NAME_ONLY_SCORE
        return [self._candidate(ref, score, stage) for ref in self._by_name.get(name, ())]

    def _by_fuzzy(self, target: Record) -> list[Candidate]:
        name = target.match_key
        if not name or not self._names or not self.config.use_fuzzy:
            return []
        matches = process.extract(
            name,
            self._names,
            scorer=fuzz.token_set_ratio,
            score_cutoff=self.config.fuzzy_threshold,
            limit=25,
        )
        return [
            self._candidate(self._name_refs[index], float(ratio), STAGE_FUZZY)
            for _, ratio, index in matches
        ]

    def _apply_volume_gate(
        self,
        target: Record,
        candidates: Sequence[Candidate],
        enforce: bool = True,
    ) -> tuple[list[Candidate], list[Candidate]]:
        """Разделяет кандидатов по совпадению объёма.

        Прежняя версия подставляла первого попавшегося кандидата, из-за чего
        строка на 50 мл получала артикул и цену варианта на 10 мл.
        """
        accepted: list[Candidate] = []
        rejected: list[Candidate] = []
        for candidate in candidates:
            conflict = self._volume_conflict(target.quantity, candidate.record.quantity)
            candidate.volume_conflict = conflict
            if conflict and enforce and self.config.enforce_volume:
                rejected.append(candidate)
            else:
                candidate.penalty = self._refinement(target, candidate)
                candidate.score -= candidate.penalty
                accepted.append(candidate)
        accepted.sort(key=lambda c: (-c.score, c.catalog, c.record.row))
        rejected.sort(key=lambda c: (-c.score, c.catalog, c.record.row))
        return accepted, rejected

    def _refinement(self, target: Record, candidate: Candidate) -> float:
        """Штраф за неточность внутри допуска: ближний вариант должен выигрывать.

        Ячейка артикула 1С перечисляет варианты одной номенклатуры, и все они
        попадают в кандидаты с одинаковой оценкой 100. Допуска объёма мало:
        «195 мл» и «200 мл, банка» отличаются на 2,5 % и проходят оба. Разводит
        их близость объёма, а при равном объёме — сходство названия.
        """
        penalty = 0.0
        if self.config.volume_penalty:
            distance = self._volume_distance(target.quantity, candidate.record.quantity)
            if distance is not None and self.config.volume_tolerance > 0:
                penalty += self.config.volume_penalty * min(
                    distance / self.config.volume_tolerance, 1.0)
        if self.config.name_penalty and target.match_key and candidate.record.match_key:
            similarity = fuzz.token_set_ratio(target.match_key, candidate.record.match_key)
            penalty += self.config.name_penalty * (1.0 - similarity / 100.0)
        return penalty

    @staticmethod
    def _volume_distance(target: Quantity | None, source: Quantity | None) -> float | None:
        """Относительное расхождение объёмов; None — сравнивать нечего."""
        if target is None or source is None or target.unit != source.unit:
            return None
        largest = max(abs(target.value), abs(source.value))
        return 0.0 if largest == 0 else abs(target.value - source.value) / largest

    def _volume_conflict(self, target: Quantity | None, source: Quantity | None) -> bool:
        """Конфликт возможен, только если объём известен с обеих сторон."""
        if target is None or source is None:
            return False
        return not target.matches(source, self.config.volume_tolerance)

    def _finalize(
        self,
        result: MatchResult,
        accepted: list[Candidate],
        rejected: list[Candidate],
    ) -> MatchResult:
        best = accepted[0]
        result.candidate = best
        result.alternatives = (accepted[1:] + rejected)[: self.config.max_alternatives]
        result.status = self._status(best, accepted)
        return result

    def _status(self, best: Candidate, accepted: Sequence[Candidate]) -> MatchStatus:
        if best.volume_conflict:
            return MatchStatus.REVIEW
        rival = accepted[1] if len(accepted) > 1 else None
        if rival and abs(best.score - rival.score) <= self.config.ambiguity_delta:
            return MatchStatus.AMBIGUOUS
        return MatchStatus.MATCHED if best.score >= self.config.auto_accept else MatchStatus.REVIEW


def build_updates(
    results: Iterable[MatchResult],
    target_sheet: Sheet,
    roles: Sequence[FieldRole],
) -> list[tuple[int, int, object]]:
    """Готовит ячейки для записи: строка, колонка (1-based) и значение из каталога."""
    columns = {role: target_sheet.column_for(role) for role in roles}
    updates: list[tuple[int, int, object]] = []
    for result in results:
        source = result.source
        if source is None or result.status is MatchStatus.NOT_FOUND:
            continue
        for role, column in columns.items():
            if column is None:
                continue
            if (value := source.by_role.get(role)) is not None:
                updates.append((result.target.row, column.index + 1, value))
    return updates


def summarize(results: Iterable[MatchResult]) -> dict[MatchStatus, int]:
    counts = {status: 0 for status in MatchStatus}
    for result in results:
        counts[result.status] += 1
    return counts
