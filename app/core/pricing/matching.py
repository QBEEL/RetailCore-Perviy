"""Сопоставление строк шаблона 1С со строками прайса поставщика.

Своего движка здесь нет: работает общий `Matcher` приложения, которому заданы
настройки переоценки. Отличий от вкладки «Сопоставление» два, и оба нужны
именно здесь.

**Ячейка артикула 1С перечисляет варианты одной номенклатуры** —
``zrp0050perG32/zrp0010perG32`` — и одна и та же ячейка стоит в нескольких
строках, различающихся только объёмом в названии. Поэтому включается штраф за
расхождение объёма: точного попадания в допуск мало, побеждать должен ближайший
вариант, а при равном объёме — более похожий по названию.

**Нечёткое совпадение никогда не применяется молча.** Если артикул у поставщика
отсутствует (позиция снята), похожие названия — это соседний аромат той же
линейки, и подстановка его цены была бы тихой ошибкой. Такие строки уходят в
«Требует сопоставления» со списком вариантов.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from ..article import DEFAULT_MODIFIER_SEPARATORS, DEFAULT_SEPARATORS
from ..matching import STAGE_ARTICLE, STAGE_EAN, MatchConfig, Matcher
from ..models import Candidate, FieldRole, MatchResult, MatchStatus
from ..normalize import KeyOptions
from .models import PriceLine, PriceStatus
from .onec import OneCTemplate
from .supplier import SupplierPrice

ProgressCallback = Callable[[int, int], None]

# Насколько сильно расхождение объёма и названия сдвигает оценку кандидата.
# Значения подобраны так, чтобы разница между ближним и дальним вариантом
# уверенно превышала порог неоднозначности (2 балла), а точное попадание
# по-прежнему давало 100.
VOLUME_PENALTY = 6.0
NAME_PENALTY = 4.0

# Этапы, результату которых можно доверять без подтверждения человеком.
_TRUSTED_STAGES = frozenset({STAGE_ARTICLE, STAGE_EAN})


@dataclass(slots=True)
class MatchOptions:
    """Настройки поиска товара — то, что показано чекбоксами на странице."""

    use_article: bool = True
    use_ean: bool = False
    use_sku: bool = True
    use_name: bool = True
    use_fuzzy: bool = True
    ignore_case: bool = True
    ignore_spaces: bool = True
    ignore_symbols: bool = True
    min_score: float = 90.0
    separators: str = DEFAULT_SEPARATORS
    modifier_separators: str = DEFAULT_MODIFIER_SEPARATORS
    volume_tolerance: float = 0.05
    max_alternatives: int = 8

    def as_match_config(self) -> MatchConfig:
        return MatchConfig(
            volume_tolerance=self.volume_tolerance,
            enforce_volume=True,
            auto_accept=self.min_score,
            max_alternatives=self.max_alternatives,
            use_article=self.use_article,
            use_sku=self.use_sku,
            use_ean=self.use_ean,
            use_name=self.use_name,
            use_fuzzy=self.use_fuzzy,
            separators=self.separators,
            modifier_separators=self.modifier_separators,
            key_options=KeyOptions(
                ignore_case=self.ignore_case,
                ignore_spaces=self.ignore_spaces,
                ignore_symbols=self.ignore_symbols,
            ),
            volume_penalty=VOLUME_PENALTY,
            name_penalty=NAME_PENALTY,
        )


def match_lines(
    template: OneCTemplate,
    supplier: SupplierPrice,
    options: MatchOptions | None = None,
    progress: ProgressCallback | None = None,
) -> list[PriceLine]:
    """Строит строки переоценки: каждая строка шаблона с найденным вариантом."""
    options = options or MatchOptions()
    matcher = Matcher(supplier.as_sheet(), options.as_match_config())
    results = matcher.match_all(template.records, progress)

    lines: list[PriceLine] = []
    for record, result in zip(template.records, results):
        line = PriceLine(
            record=record,
            row=record.row,
            article=record.text(FieldRole.ARTICLE),
            name=record.text(FieldRole.NAME) or record.label,
            quantity=record.quantity,
            alternatives=list(result.alternatives),
        )
        if result.candidate is not None and _trusted(result, options.min_score):
            line.candidate = result.candidate
            line.status = PriceStatus.UNCHANGED
        else:
            if result.candidate is not None:
                line.alternatives.insert(0, result.candidate)
            line.status = PriceStatus.REVIEW if line.alternatives else PriceStatus.NOT_FOUND
        lines.append(line)
    return lines


def _trusted(result: MatchResult, min_score: float) -> bool:
    """Кандидат применяется сам, только если это надёжный этап без спора.

    Совпадение по названию и нечёткое сравнение всегда требуют подтверждения:
    цена чужого товара хуже, чем пустая строка.
    """
    if result.status in (MatchStatus.AMBIGUOUS, MatchStatus.REVIEW):
        return False
    candidate = result.candidate
    return (
        candidate is not None
        and candidate.stage in _TRUSTED_STAGES
        and not candidate.volume_conflict
        # Порог сверяется с оценкой этапа: уточнение по объёму и названию лишь
        # разводит кандидатов, и опускать из-за него надёжный артикул нельзя.
        and result.stage_score >= min_score
    )


def choices(line: PriceLine, limit: int = 8) -> Sequence[Candidate]:
    """Варианты для показа пользователю: текущий выбор первым."""
    chosen = [line.candidate] if line.candidate else []
    return (chosen + [c for c in line.alternatives if c is not line.candidate])[:limit]
