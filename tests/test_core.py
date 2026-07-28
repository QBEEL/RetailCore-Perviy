"""Тесты ядра: нормализация, поиск, сопоставление."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.matching import MatchConfig, Matcher
from app.core.models import Column, FieldRole, MatchStatus, Quantity, Record, Sheet
from app.core.normalize import (
    code_key,
    comparable,
    detect_noise_tokens,
    digits_only,
    extract_quantity,
    normalize_text,
    parse_quantity,
    split_multi,
)
from app.core.schema import detect_columns, detect_header_row
from app.core.search import SearchEngine, SearchConfig
from app.core.workbook import _prepare


# --- нормализация ------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "value", "unit"),
    [
        ("200 мл, банка", 200, "мл"),   # кириллица + лишние слова
        ("10 мл", 10, "мл"),
        ("212,5мл", 212.5, "мл"),       # запятая как разделитель дробной части
        ("75гр", 75, "г"),
        ("1 л", 1000, "мл"),            # приведение к базовой единице
        ("2 kg", 2000, "г"),
        ("100 ml", 100, "мл"),          # латиница работает так же
    ],
)
def test_parse_quantity(text: str, value: float, unit: str) -> None:
    quantity = parse_quantity(text)
    assert quantity == Quantity(value, unit)


@pytest.mark.parametrize("text", ["2 подсвечника", "набор", "", None, "abc"])
def test_parse_quantity_without_units(text: object) -> None:
    assert parse_quantity(text) is None


def test_extract_quantity_prefers_parentheses() -> None:
    assert extract_quantity("Духи Апельсин, Жасмин (50мл)") == Quantity(50, "мл")


def test_quantity_tolerance() -> None:
    # 195 мл и 200 мл — один товар, 10 мл и 50 мл — разные.
    assert Quantity(195, "мл").matches(Quantity(200, "мл"), 0.05)
    assert Quantity(212, "мл").matches(Quantity(212.5, "мл"), 0.05)
    assert not Quantity(10, "мл").matches(Quantity(50, "мл"), 0.05)
    assert not Quantity(100, "мл").matches(Quantity(100, "г"), 0.05)


def test_normalize_text_collapses_case_spaces_and_yo() -> None:
    assert normalize_text("Milk   Cleanser") == "milk cleanser"
    assert normalize_text("MILK") == normalize_text("Milk") == "milk"
    assert normalize_text("Тёмный") == "темный"


def test_detect_noise_tokens_finds_repeated_brand() -> None:
    names = [f"Крем Z&R вариант {i}" for i in range(10)]
    noise = detect_noise_tokens(names)
    assert {"z", "r"} <= noise
    assert "вариант" in noise  # тоже повторяется везде
    assert "1" not in noise


def test_comparable_drops_noise_and_volume() -> None:
    noise = frozenset({"z", "r"})
    assert comparable("Крем для тела Z&R Ветивер, Лимон (195мл)", noise) == "крем для тела ветивер лимон"


def test_helpers() -> None:
    assert digits_only("4603720459040.0") == "4603720459040"
    assert code_key(" ZRP-0050/PER ") == "zrp0050per"
    assert split_multi("a / b, c") == ["a", "b", "c"]
    assert split_multi("=FORMULA()") == []


# --- определение структуры ---------------------------------------------------

def test_detect_header_and_roles() -> None:
    rows = [
        ["EAN", "Артикул", "Номенклатура", "Наименование английское", "РРЦ"],
        [4603720459040, "zrp0050perG06", "Духи Апельсин (50мл)", "Perfume Orange (50 ml)", 7990],
    ]
    assert detect_header_row(rows) == 0
    columns = detect_columns(rows[0], rows[1:])
    roles = [c.role for c in columns]
    assert roles[:3] == [FieldRole.EAN, FieldRole.ARTICLE, FieldRole.NAME]
    # Вторая колонка с названием понижается до дополнительной, а не спорит за роль.
    assert roles[3] is FieldRole.NAME_ALT
    assert roles[4] is FieldRole.PRICE


def test_detect_columns_without_headers_uses_content() -> None:
    rows = [[None, None], ["4603720459040", "Духи концентрированные Апельсин, Жасмин"]]
    columns = detect_columns(rows[0], rows[1:])
    assert columns[0].role is FieldRole.EAN
    assert columns[1].role is FieldRole.NAME


# --- вспомогательные фабрики --------------------------------------------------

def _sheet(rows: list[dict[FieldRole, object]], noise: frozenset[str] = frozenset()) -> Sheet:
    columns = [
        Column(0, "Артикул", FieldRole.ARTICLE),
        Column(1, "EAN", FieldRole.EAN),
        Column(2, "Наименование", FieldRole.NAME),
        Column(3, "Объём", FieldRole.VOLUME),
        Column(4, "Цена", FieldRole.PRICE),
    ]
    records = []
    for index, values in enumerate(rows):
        record = Record(row=index + 2, values=list(values.values()), by_role=dict(values))
        _prepare(record, noise)
        records.append(record)
    return Sheet(path="test.xlsx", sheet_name="Лист", header_row=0, columns=columns,
                 records=records, noise_tokens=noise)


CATALOG = [
    {FieldRole.ARTICLE: "zrp0010perG06", FieldRole.EAN: "4603720459477",
     FieldRole.NAME: "Духи концентрированные Апельсин, Жасмин (10мл)", FieldRole.PRICE: 2790},
    {FieldRole.ARTICLE: "zrp0050perG06", FieldRole.EAN: "4603720459040",
     FieldRole.NAME: "Духи концентрированные Апельсин, Жасмин (50мл)", FieldRole.PRICE: 7990},
    {FieldRole.ARTICLE: "ils0200bcrG03", FieldRole.EAN: "7290018419380",
     FieldRole.NAME: "Крем для тела Ветивер, Лимон (195мл)", FieldRole.PRICE: 3990},
    {FieldRole.ARTICLE: "zrn0212difG06", FieldRole.EAN: "4627153152347",
     FieldRole.NAME: "Диффузор. Средство для ароматизации помещений Апельсин, Жасмин (212,5мл)",
     FieldRole.PRICE: 8290},
]


# --- поиск --------------------------------------------------------------------

@pytest.fixture
def engine() -> SearchEngine:
    return SearchEngine(_sheet(CATALOG), SearchConfig())


def test_search_partial_and_case_insensitive(engine: SearchEngine) -> None:
    for query in ("духи", "ДУХИ", "Духи"):
        labels = [hit.record.text(FieldRole.NAME) for hit in engine.search(query)]
        assert len(labels) >= 2
        assert all("Духи" in label for label in labels[:2])


def test_search_ignores_extra_spaces(engine: SearchEngine) -> None:
    spaced = engine.search("крем   для    тела")
    normal = engine.search("крем для тела")
    assert [h.record.row for h in spaced] == [h.record.row for h in normal]
    assert spaced


def test_search_multiple_words_requires_all(engine: SearchEngine) -> None:
    hits = engine.search("апельсин жасмин")
    assert len(hits) == 3
    hits = engine.search("крем лимон")
    assert len(hits) == 1


def test_search_tolerates_typos(engine: SearchEngine) -> None:
    hits = engine.search("Апельсн Жасмин")
    assert hits and "Апельсин" in hits[0].record.text(FieldRole.NAME)


def test_search_finds_typo_in_short_single_word(engine: SearchEngine) -> None:
    """Короткий запрос с опечаткой не должен теряться на отборе кандидатов."""
    hits = engine.search("Диффузр")
    assert hits and "Диффузор" in hits[0].record.text(FieldRole.NAME)


def test_search_by_article_scores_highest(engine: SearchEngine) -> None:
    hits = engine.search("zrp0050perG06")
    assert hits[0].record.text(FieldRole.ARTICLE) == "zrp0050perG06"
    assert hits[0].score >= 95  # артикул имеет наибольший вес


def test_search_results_sorted_by_score(engine: SearchEngine) -> None:
    scores = [hit.score for hit in engine.search("духи апельсин")]
    assert scores == sorted(scores, reverse=True)


def test_disabled_field_is_ignored() -> None:
    config = SearchConfig(roles={FieldRole.NAME})
    engine = SearchEngine(_sheet(CATALOG), config)
    assert not engine.search("zrp0050perG06")
    config.roles.add(FieldRole.ARTICLE)
    assert engine.search("zrp0050perG06")


# --- сопоставление ------------------------------------------------------------

@pytest.fixture
def matcher() -> Matcher:
    return Matcher(_sheet(CATALOG), MatchConfig())


def test_volume_gate_picks_correct_variant(matcher: Matcher) -> None:
    """Строка на 50 мл не должна получать артикул и цену варианта на 10 мл."""
    target = _sheet([{FieldRole.NAME: "Духи концентрированные Z&R Апельсин, Жасмин",
                      FieldRole.VOLUME: "50 мл"}], noise=frozenset({"z", "r"})).records[0]
    result = matcher.match(target)
    assert result.source is not None
    assert result.source.text(FieldRole.ARTICLE) == "zrp0050perG06"
    assert result.source.get(FieldRole.PRICE) == 7990


def test_volume_tolerance_matches_close_sizes(matcher: Matcher) -> None:
    target = _sheet([{FieldRole.NAME: "Крем для тела Z&R Ветивер, Лимон",
                      FieldRole.VOLUME: "200 мл, банка"}], noise=frozenset({"z", "r"})).records[0]
    result = matcher.match(target)
    assert result.source is not None and result.source.text(FieldRole.ARTICLE) == "ils0200bcrG03"


def test_different_wording_still_matches_with_volume(matcher: Matcher) -> None:
    target = _sheet([{FieldRole.NAME: "Диффузор для ароматерапии Z&R Апельсин, Жасмин",
                      FieldRole.VOLUME: "212 мл"}], noise=frozenset({"z", "r"})).records[0]
    result = matcher.match(target)
    assert result.source is not None and result.source.text(FieldRole.ARTICLE) == "zrn0212difG06"


def test_conflicting_volume_goes_to_review(matcher: Matcher) -> None:
    """Если подходящего объёма нет, привязка не подставляется молча."""
    target = _sheet([{FieldRole.NAME: "Духи концентрированные Z&R Апельсин, Жасмин",
                      FieldRole.VOLUME: "500 мл"}], noise=frozenset({"z", "r"})).records[0]
    result = matcher.match(target)
    assert result.source is None
    assert result.status is MatchStatus.REVIEW
    assert result.alternatives  # варианты показываются пользователю


def test_identifier_wins_over_name(matcher: Matcher) -> None:
    target = _sheet([{FieldRole.ARTICLE: "ils0200bcrG03", FieldRole.NAME: "Совсем другое название"}]).records[0]
    result = matcher.match(target)
    assert result.status is MatchStatus.MATCHED
    assert result.source.text(FieldRole.EAN) == "7290018419380"


def test_ean_matches_numeric_value(matcher: Matcher) -> None:
    target = _sheet([{FieldRole.EAN: 4603720459040, FieldRole.NAME: "неизвестно"}]).records[0]
    result = matcher.match(target)
    assert result.source is not None and result.source.text(FieldRole.ARTICLE) == "zrp0050perG06"


def test_unknown_row_is_not_found(matcher: Matcher) -> None:
    target = _sheet([{FieldRole.NAME: "Совершенно посторонний товар щетка"}]).records[0]
    result = matcher.match(target)
    assert result.source is None
    assert result.status is MatchStatus.NOT_FOUND


# --- несколько каталогов ------------------------------------------------------

ARCHIVE = [
    {FieldRole.ARTICLE: "ils0200scrP11", FieldRole.EAN: "7290116440101",
     FieldRole.NAME: "Скраб для тела Ирис, Лилия, Ваниль (200мл)", FieldRole.PRICE: 3390},
    {FieldRole.ARTICLE: "OLD-ORANGE-50", FieldRole.EAN: "1111111111111",
     FieldRole.NAME: "Духи концентрированные Апельсин, Жасмин (50мл)", FieldRole.PRICE: 6990},
]


def _multi() -> Matcher:
    main = _sheet(CATALOG)
    main.path = "main.xlsx"
    archive = _sheet(ARCHIVE)
    archive.path = "archive.xlsx"
    return Matcher([main, archive], MatchConfig())


def test_second_catalog_covers_gap_of_the_first() -> None:
    """Позиция, которой нет в основном прайсе, находится в дополнительном."""
    target = _sheet([{FieldRole.NAME: "Скраб для тела Z&R Ирис, Лилия, Ваниль",
                      FieldRole.VOLUME: "200 мл"}], noise=frozenset({"z", "r"})).records[0]
    result = _multi().match(target)
    assert result.status is MatchStatus.MATCHED
    assert result.source.text(FieldRole.ARTICLE) == "ils0200scrP11"
    assert result.origin == "archive.xlsx"


def test_primary_catalog_wins_on_equal_score() -> None:
    """При одинаковой оценке приоритет у каталога выше в списке."""
    target = _sheet([{FieldRole.NAME: "Духи концентрированные Z&R Апельсин, Жасмин",
                      FieldRole.VOLUME: "50 мл"}], noise=frozenset({"z", "r"})).records[0]
    result = _multi().match(target)
    assert result.source.text(FieldRole.ARTICLE) == "zrp0050perG06"
    assert result.origin == "main.xlsx"
    # Запись из дополнительного каталога остаётся доступной как альтернатива.
    assert any(c.origin == "archive.xlsx" for c in result.alternatives)


def test_single_sheet_still_accepted() -> None:
    assert Matcher(_sheet(CATALOG)).sources[0].records


def test_works_without_any_brand_prefix(matcher: Matcher) -> None:
    """Универсальность: файл другого бренда обрабатывается без спец-условий."""
    target = _sheet([{FieldRole.NAME: "Крем для тела ACME Ветивер, Лимон", FieldRole.VOLUME: "195 мл"}],
                    noise=frozenset({"acme"})).records[0]
    result = matcher.match(target)
    assert result.source is not None and result.source.text(FieldRole.ARTICLE) == "ils0200bcrG03"
