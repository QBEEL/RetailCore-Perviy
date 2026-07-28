"""Умный поиск: частичное совпадение, регистр, многословность, опечатки, Match Score."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from rapidfuzz import fuzz, process

from .models import DEFAULT_SEARCH_ROLES, DEFAULT_WEIGHTS, FieldRole, Record, Sheet
from .normalize import normalize_text

# Тиры оценки. Порядок убывания надёжности совпадения.
TIER_EXACT = 100.0
TIER_CASELESS = 95.0
TIER_PREFIX = 90.0
TIER_SUBSTRING = 88.0
TIER_ALL_TERMS = 85.0
TIER_FUZZY = 80.0

TIER_TITLES = {
    TIER_EXACT: "точное",
    TIER_CASELESS: "без учёта регистра",
    TIER_PREFIX: "по началу",
    TIER_SUBSTRING: "частичное",
    TIER_ALL_TERMS: "по словам",
    TIER_FUZZY: "нечёткое",
}


@dataclass(slots=True)
class SearchConfig:
    """Настройки поиска. Сохраняются между запусками."""

    roles: set[FieldRole] = field(default_factory=lambda: set(DEFAULT_SEARCH_ROLES))
    weights: dict[FieldRole, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    fuzzy_enabled: bool = True
    fuzzy_threshold: float = 75.0
    min_score: float = 35.0
    limit: int = 200

    def weight(self, role: FieldRole) -> float:
        return self.weights.get(role, DEFAULT_WEIGHTS.get(role, 0.5))

    def enabled(self, role: FieldRole) -> bool:
        return role in self.roles


@dataclass(slots=True)
class SearchHit:
    record: Record
    score: float
    role: FieldRole
    tier: float
    terms: list[str]

    @property
    def explanation(self) -> str:
        return f"{tier_title(self.tier)} · {self.role.title}"


class SearchEngine:
    """Индексирует лист один раз и переиспользует нормализованные представления."""

    def __init__(self, sheet: Sheet | None = None, config: SearchConfig | None = None) -> None:
        self.config = config or SearchConfig()
        self._records: list[Record] = []
        self._blobs: list[str] = []
        if sheet is not None:
            self.index(sheet)

    def index(self, sheet: Sheet | None) -> None:
        self._records = list(sheet.records) if sheet else []
        self._blobs = [r.search_blob for r in self._records]

    @property
    def records(self) -> Sequence[Record]:
        return self._records

    def search(self, query: str, limit: int | None = None) -> list[SearchHit]:
        terms = normalize_text(query).split()
        if not terms:
            return []
        query_norm = " ".join(terms)
        query_raw = query.strip()
        limit = limit or self.config.limit

        candidates = self._candidates(terms, query_norm)
        hits: list[SearchHit] = []
        for index in candidates:
            record = self._records[index]
            scored = self._score(record, query_raw, query_norm, terms)
            if scored is not None and scored.score >= self.config.min_score:
                hits.append(scored)

        hits.sort(key=lambda h: (-h.score, h.record.row))
        return hits[:limit]

    def _candidates(self, terms: Sequence[str], query_norm: str) -> list[int]:
        """Сначала дешёвый отбор по подстрокам, затем нечёткий добор через rapidfuzz.

        Нечёткий проход по всему каталогу пропускается, если точных совпадений уже
        достаточно: их тиры всё равно выше любого нечёткого результата.
        """
        selected = [i for i, blob in enumerate(self._blobs) if all(term in blob for term in terms)]
        if not self.config.fuzzy_enabled or len(selected) >= self.config.limit:
            return selected

        seen = set(selected)
        # partial_ratio, а не WRatio: последний штрафует короткий запрос против
        # длинной строки полей, и «clenser» переставал находить «Cleanser».
        matches = process.extract(
            query_norm,
            self._blobs,
            scorer=fuzz.partial_ratio,
            score_cutoff=self.config.fuzzy_threshold,
            limit=self.config.limit * 3,
        )
        selected.extend(index for _, _, index in matches if index not in seen)
        return selected

    def _score(self, record: Record, query_raw: str, query_norm: str, terms: Sequence[str]) -> SearchHit | None:
        best: SearchHit | None = None
        matched_fields = 0
        for role in self.config.roles:
            normalized = record.normalized.get(role)
            if not normalized:
                continue
            tier = self._tier(record.text(role), normalized, query_raw, query_norm, terms)
            if tier <= 0:
                continue
            matched_fields += 1
            score = tier * self.config.weight(role)
            if best is None or score > best.score:
                best = SearchHit(record=record, score=score, role=role, tier=tier, terms=list(terms))
        if best is None:
            return None
        # Совпадение сразу в нескольких полях — дополнительное подтверждение.
        best.score = min(100.0, best.score + min(matched_fields - 1, 3) * 1.5)
        return best

    def _tier(
        self,
        raw: str,
        normalized: str,
        query_raw: str,
        query_norm: str,
        terms: Sequence[str],
    ) -> float:
        if raw == query_raw:
            return TIER_EXACT
        if normalized == query_norm:
            return TIER_CASELESS
        if normalized.startswith(query_norm):
            return TIER_PREFIX
        if query_norm in normalized:
            return TIER_SUBSTRING
        if len(terms) > 1 and all(term in normalized for term in terms):
            return TIER_ALL_TERMS
        if self.config.fuzzy_enabled:
            similarity = _term_similarity(terms, normalized)
            if similarity >= self.config.fuzzy_threshold:
                return TIER_FUZZY * similarity / 100.0
        return 0.0


def _term_similarity(terms: Sequence[str], normalized: str) -> float:
    """Средняя близость каждого слова запроса к ближайшему слову поля.

    Оценка по каждому слову отдельно, а не по строке целиком: иначе запись,
    совпавшая лишь одним словом из двух, получала бы ту же оценку, что и запись,
    совпавшая обоими.
    """
    tokens = normalized.split()
    if not tokens:
        return 0.0
    total = 0.0
    for term in terms:
        if term in normalized:
            total += 100.0
        else:
            total += max(fuzz.ratio(term, token) for token in tokens)
    return total / len(terms)


def tier_title(tier: float) -> str:
    for threshold, title in ((TIER_EXACT, TIER_TITLES[TIER_EXACT]),
                             (TIER_CASELESS, TIER_TITLES[TIER_CASELESS]),
                             (TIER_PREFIX, TIER_TITLES[TIER_PREFIX]),
                             (TIER_SUBSTRING, TIER_TITLES[TIER_SUBSTRING]),
                             (TIER_ALL_TERMS, TIER_TITLES[TIER_ALL_TERMS])):
        if tier >= threshold:
            return title
    return TIER_TITLES[TIER_FUZZY]


def highlight_terms(text: str, terms: Iterable[str]) -> list[tuple[int, int]]:
    """Диапазоны совпадений для подсветки в таблице."""
    if not text:
        return []
    haystack = text.casefold().replace("ё", "е")
    spans: list[tuple[int, int]] = []
    for term in terms:
        start = haystack.find(term)
        while start != -1:
            spans.append((start, start + len(term)))
            start = haystack.find(term, start + len(term))
    return _merge(spans)


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
