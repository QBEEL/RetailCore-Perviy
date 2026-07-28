"""Перенос количеств заказа из выгрузки 1С в бланк заказа поставщика.

Ключ переноса — исключение, заданное пользователем, затем артикул и штрихкод.
Совпадение по названию не применяется автоматически: в бланке рядом стоят
обычная позиция и ТЕСТЕР, у которых артикулы отличаются одной буквой, и заказ
ушёл бы не в ту строку. Для случаев, когда номенклатура 1С и прайс поставщика
описывают товар по-разному, служат исключения — сохранённые ручные привязки.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Container, Iterable, Sequence

from rapidfuzz import fuzz, process

from .normalize import code_key, comparable, digits_only, normalize_text
from .workbook import list_sheets, read_raw

# Заголовки, по которым распознаются колонки.
_ORDER_WORDS = ("заказ", "к заказу", "order")
_ARTICLE_WORDS = ("артикул", "article")
_EAN_WORDS = ("штрихкод", "штрих код", "ean", "barcode", "gtin")
_NAME_WORDS = ("номенклатура", "наименование", "товар", "продукт", "name")
_TRAIT_WORDS = ("характеристика", "вариант", "модификация")
_HEADER_SCAN_ROWS = 16
_TESTER_MARK = "тестер"

# Пометки поставщика. Сравниваются как отдельные слова: «силиконовый» не должен
# стать новинкой из-за подстроки «новый», а «Новый артикул» — это не пометка.
_MARK_TOKENS = {
    "новинка": "НОВИНКА", "новинки": "НОВИНКА", "new": "NEW",
    "хит": "ХИТ", "хиты": "ХИТ", "hit": "HIT",
    "лимитка": "ЛИМИТКА", "лимитки": "ЛИМИТКА",
    "limited": "LIMITED", "лимитед": "LIMITED",
}


@dataclass(slots=True)
class ColumnOption:
    """Колонка-кандидат на роль «заказ» с числом заполненных значений."""

    index: int
    title: str
    filled: int

    @property
    def label(self) -> str:
        return f"{self.title} ({self.filled} знач.)" if self.filled else f"{self.title} — пусто"


@dataclass(slots=True)
class OrderSheet:
    """Разобранный лист: где заголовок, где артикул, штрихкод, название и заказ."""

    path: str
    sheet: str
    header_row: int
    rows: list[list[Any]]
    titles: list[str]
    article: int | None = None
    ean: int | None = None
    name: int | None = None
    trait: int | None = None
    quantity: int | None = None
    options: list[ColumnOption] = field(default_factory=list)

    @property
    def first_data_row(self) -> int:
        return self.header_row + 1

    def excel_row(self, index: int) -> int:
        """Номер строки в Excel (1-based) для строки списка `rows`."""
        return index + 1

    def value(self, index: int, column: int | None) -> Any:
        if column is None or index >= len(self.rows):
            return None
        row = self.rows[index]
        return row[column] if column < len(row) else None

    def title_of(self, column: int | None) -> str:
        if column is None or column >= len(self.titles):
            return ""
        return self.titles[column]


@dataclass(slots=True)
class TargetRow:
    """Строка бланка, пригодная для привязки."""

    row: int
    article: str
    ean: str
    name: str
    tester: bool
    blob: str = ""
    trait: str = ""
    marks: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return f"{self.name} · {self.trait}" if self.trait else self.name


@dataclass(slots=True)
class Suggestion:
    """Похожая строка бланка для ручной привязки."""

    row: int
    article: str
    name: str
    score: float
    tester: bool
    ean: str = ""
    marks: tuple[str, ...] = ()

    @classmethod
    def of(cls, entry: "TargetRow", score: float) -> "Suggestion":
        return cls(entry.row, entry.article, entry.title, score, entry.tester, entry.ean, entry.marks)


@dataclass(slots=True)
class Alias:
    """Исключение: позиция 1С и строка бланка, названия которых не совпадают.

    Хранится не номером строки, а артикулом, штрихкодом и названием: в новом
    файле поставщика позиция окажется в другой строке, а привязка сохранится.
    """

    source_article: str = ""
    source_ean: str = ""
    source_name: str = ""
    target_article: str = ""
    target_ean: str = ""
    target_name: str = ""
    source_trait: str = ""

    def keys(self) -> list[str]:
        """Ключи, по которым исключение находится для позиции заказа."""
        keys = []
        if code := code_key(self.source_article):
            keys.append(code)
        if self.source_ean:
            keys.append(f"e:{self.source_ean}")
        # Характеристика входит в ключ: у оттенков 01 и 02 название одинаковое,
        # и без неё исключение для одного применилось бы ко всем.
        keys += _name_keys(self.source_name, self.source_trait)
        return keys

    def identity(self) -> set[str]:
        """Ключи, по которым исключение считается тем же самым при замене.

        Общее название без характеристики сюда не входит: иначе привязка для
        оттенка 02 удалила бы привязку оттенка 01.
        """
        keys = {key for key in self.keys() if not key.startswith("n:")}
        if precise := _name_keys(self.source_name, self.source_trait):
            keys.add(precise[0])
        return keys

    @property
    def title(self) -> str:
        source = self.source_name or self.source_article or self.source_ean
        target = self.target_name or self.target_article or self.target_ean
        return f"{source}  →  {target}"

    def as_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in _ALIAS_FIELDS}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Alias":
        return cls(**{name: str(data.get(name) or "") for name in _ALIAS_FIELDS})


class AliasBook:
    """Список исключений с поиском по артикулу, штрихкоду и названию."""

    def __init__(self, items: Iterable[Alias] = ()) -> None:
        self.items: list[Alias] = [a for a in items if a.keys()]
        self._reindex()

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    def _reindex(self) -> None:
        self._by_key: dict[str, Alias] = {}
        for alias in self.items:
            for key in alias.keys():
                self._by_key.setdefault(key, alias)

    def find(self, line: "OrderLine") -> Alias | None:
        for key in _line_keys(line):
            if alias := self._by_key.get(key):
                return alias
        return None

    def remember(self, line: "OrderLine", entry: TargetRow) -> Alias:
        """Запоминает ручную привязку. Прежнее исключение для позиции заменяется."""
        alias = Alias(
            source_article=line.article,
            source_ean=line.ean,
            source_name=line.name,
            source_trait=line.trait,
            target_article=entry.article,
            target_ean=entry.ean,
            target_name=entry.name,
        )
        keys = alias.identity()
        self.items = [old for old in self.items if not keys.intersection(old.identity())]
        self.items.append(alias)
        self._reindex()
        return alias

    def forget(self, alias: Alias) -> None:
        self.items = [item for item in self.items if item is not alias]
        self._reindex()

    def clear(self) -> None:
        self.items = []
        self._reindex()


_ALIAS_FIELDS = (
    "source_article", "source_ean", "source_name", "source_trait",
    "target_article", "target_ean", "target_name",
)


def _name_keys(name: str, trait: str) -> list[str]:
    """Ключ с характеристикой и без неё: исключения, сохранённые до появления
    характеристики, должны продолжать работать."""
    keys = []
    if trait and (full := comparable(f"{name} {trait}")):
        keys.append(f"n:{full}")
    if plain := comparable(name):
        keys.append(f"n:{plain}")
    return keys


def _line_keys(line: "OrderLine") -> list[str]:
    keys = []
    if code := code_key(line.article):
        keys.append(code)
    if line.ean:
        keys.append(f"e:{line.ean}")
    return keys + _name_keys(line.name, line.trait)


@dataclass(slots=True)
class OrderLine:
    """Одна позиция заказа и её судьба при переносе."""

    source_row: int
    article: str
    ean: str
    name: str
    quantity: float
    trait: str = ""
    target_row: int | None = None
    method: str = ""
    suggestions: list[Suggestion] = field(default_factory=list)

    @classmethod
    def from_target(cls, entry: TargetRow, quantity: float) -> "OrderLine":
        """Позиция, добавленная из прайса, а не из выгрузки: новинка, хит, лимитка."""
        line = cls(source_row=0, article=entry.article, ean=entry.ean, name=entry.name,
                   quantity=float(quantity), trait=entry.trait)
        line.target_row = entry.row
        line.method = "/".join(entry.marks) or "Добавлено"
        return line

    @property
    def matched(self) -> bool:
        return self.target_row is not None

    @property
    def added(self) -> bool:
        """Позиция взята из прайса, а не из выгрузки 1С."""
        return self.source_row == 0

    @property
    def title(self) -> str:
        return f"{self.name} · {self.trait}" if self.trait else self.name

    def assign(self, row: int) -> None:
        self.target_row = row
        self.method = "Вручную"

    def clear(self) -> None:
        self.target_row = None
        self.method = ""


# --- разбор листов ------------------------------------------------------------

def _header_row(rows: Sequence[Sequence[Any]]) -> int:
    """Строка заголовка — с наибольшим числом коротких текстовых подписей."""
    best, best_score = 0, -1
    for index, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        labels = sum(1 for v in row if isinstance(v, str) and 0 < len(v.strip()) <= 60)
        if labels > best_score:
            best, best_score = index, labels
    return best


def _titles(rows: Sequence[Sequence[Any]], header: int) -> list[str]:
    """Заголовок колонки: подпись плюс группа строкой выше (двухуровневая шапка 1С)."""
    width = max((len(r) for r in rows[: header + 2]), default=0)
    above = rows[header - 1] if header > 0 else []
    current = rows[header] if header < len(rows) else []
    titles: list[str] = []
    for column in range(width):
        top = _text(above[column] if column < len(above) else None)
        bottom = _text(current[column] if column < len(current) else None)
        titles.append(" · ".join(p for p in (top, bottom) if p))
    return titles


def _text(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split())


def _match_header(titles: Sequence[str], words: Sequence[str]) -> list[int]:
    return [
        index for index, title in enumerate(titles)
        if any(word in normalize_text(title) for word in words)
    ]


def _count_numeric(rows: Sequence[Sequence[Any]], column: int, start: int) -> int:
    total = 0
    for row in rows[start:]:
        if column < len(row):
            value = row[column]
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value:
                total += 1
    return total


def _count_codes(rows: Sequence[Sequence[Any]], column: int, start: int) -> int:
    """Сколько значений похожи на штрихкод: 8–14 цифр."""
    total = 0
    for row in rows[start:]:
        if column < len(row):
            digits = digits_only(row[column])
            if 8 <= len(digits) <= 14:
                total += 1
    return total


def _count_text(rows: Sequence[Sequence[Any]], column: int, start: int) -> int:
    return sum(
        1 for row in rows[start:]
        if column < len(row) and isinstance(row[column], str) and row[column].strip()
    )


def _count_values(rows: Sequence[Sequence[Any]], column: int, start: int) -> int:
    """Любые непустые значения. Артикул бывает числом — в прайсах туда пишут штрихкод."""
    return sum(
        1 for row in rows[start:]
        if column < len(row) and row[column] is not None and str(row[column]).strip()
    )


def parse_sheet(path: str, sheet: str | None = None) -> OrderSheet:
    """Читает лист и определяет нужные колонки, сверяя заголовки с данными."""
    rows, resolved = read_raw(path, sheet)
    header = _header_row(rows)
    titles = _titles(rows, header)
    start = header + 1

    parsed = OrderSheet(path=path, sheet=resolved, header_row=header, rows=rows, titles=titles)
    parsed.article = _best(_match_header(titles, _ARTICLE_WORDS), rows, start, _count_values)
    parsed.name = _best(_match_header(titles, _NAME_WORDS), rows, start, _count_text)
    parsed.trait = _best(_match_header(titles, _TRAIT_WORDS), rows, start, _count_text)

    # Заголовок «Штрихкод» может стоять над служебной колонкой, а сами коды —
    # в соседней, поэтому кандидаты проверяются по содержимому.
    ean_candidates = _match_header(titles, _EAN_WORDS)
    parsed.ean = _best(ean_candidates, rows, start, _count_codes)
    if parsed.ean is None or _count_codes(rows, parsed.ean, start) == 0:
        width = len(titles)
        parsed.ean = _best(list(range(width)), rows, start, _count_codes)

    parsed.options = [
        ColumnOption(index, titles[index] or f"Колонка {index + 1}", _count_numeric(rows, index, start))
        for index in _match_header(titles, _ORDER_WORDS)
    ]
    # Из нескольких колонок «Заказ» (в 1С они есть у каждого магазина) берётся
    # та, где реально есть числа. Если чисел нет нигде — та, чей заголовок про
    # заказ и говорит: «ЗАКАЗ, шт» важнее «Минимальное количество для оптового
    # заказа/кратность», где слово попало случайно.
    parsed.options.sort(key=lambda o: (-o.filled, len(normalize_text(o.title).split()), o.index))
    parsed.quantity = parsed.options[0].index if parsed.options else None
    return parsed


def _best(candidates: Sequence[int], rows, start: int, counter) -> int | None:
    scored = [(counter(rows, index, start), index) for index in candidates]
    scored = [pair for pair in scored if pair[0] > 0]
    if not scored:
        return None
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return scored[0][1]


def detect_source(path: str) -> OrderSheet:
    """Лист выгрузки, где колонка заказа действительно заполнена."""
    best: OrderSheet | None = None
    for sheet in list_sheets(path):
        parsed = parse_sheet(path, sheet)
        filled = parsed.options[0].filled if parsed.options else 0
        if best is None or filled > (best.options[0].filled if best.options else 0):
            best = parsed
    if best is None:
        raise ValueError("В файле нет листов")
    return best


def detect_target(path: str) -> OrderSheet:
    """Лист бланка: тот, где есть артикулы и колонка «Заказ»."""
    fallback: OrderSheet | None = None
    for sheet in list_sheets(path):
        parsed = parse_sheet(path, sheet)
        if parsed.article is not None and parsed.quantity is not None:
            return parsed
        fallback = fallback or parsed
    if fallback is None:
        raise ValueError("В файле нет листов")
    return fallback


def open_sheet(path: str, sheet: str | None = None, *, source: bool) -> OrderSheet:
    """Явно выбранный лист, а если не выбран — определённый автоматически."""
    if sheet:
        return parse_sheet(path, sheet)
    return detect_source(path) if source else detect_target(path)


# --- перенос ------------------------------------------------------------------

def read_orders(source: OrderSheet) -> list[OrderLine]:
    """Позиции с ненулевым количеством заказа."""
    lines: list[OrderLine] = []
    for index in range(source.first_data_row, len(source.rows)):
        quantity = source.value(index, source.quantity)
        if not isinstance(quantity, (int, float)) or isinstance(quantity, bool) or not quantity:
            continue
        article = _text(source.value(index, source.article))
        ean = digits_only(source.value(index, source.ean))
        name = _text(source.value(index, source.name))
        if not (article or ean):
            continue
        lines.append(OrderLine(
            source_row=source.excel_row(index),
            article=article,
            ean=ean,
            name=name,
            trait=_text(source.value(index, source.trait)),
            quantity=float(quantity),
        ))
    return lines


class TargetIndex:
    """Индексы бланка для поиска строки по артикулу, штрихкоду и названию."""

    def __init__(self, target: OrderSheet) -> None:
        self.target = target
        self.entries: list[TargetRow] = []
        self.by_row: dict[int, TargetRow] = {}
        self.by_code: dict[str, int] = {}
        self.by_ean: dict[str, int] = {}
        self.by_name: dict[str, int] = {}
        self.testers: set[int] = set()
        self._names: list[str] = []
        self._rows: list[int] = []

        for index in range(target.first_data_row, len(target.rows)):
            article = _text(target.value(index, target.article))
            ean = digits_only(target.value(index, target.ean))
            name = _text(target.value(index, target.name))
            if not (article or ean):
                continue
            row = target.excel_row(index)
            trait = _text(target.value(index, target.trait))
            entry = TargetRow(
                row, article, ean, name, is_tester(article, name),
                normalize_text(f"{article} {ean} {name} {trait}"),
                trait, detect_marks(target.rows[index]),
            )
            self.entries.append(entry)
            self.by_row[row] = entry
            if entry.tester:
                self.testers.add(row)
            if key := code_key(article):
                self.by_code.setdefault(key, row)
            if ean:
                self.by_ean.setdefault(ean, row)
            if name:
                self.by_name.setdefault(comparable(name), row)
                self._names.append(comparable(f"{name} {trait}"))
                self._rows.append(row)

    def info(self, row: int) -> TargetRow:
        return self.by_row.get(row) or TargetRow(row, "", "", "", False)

    def locate(self, alias: Alias) -> int | None:
        """Строка бланка, на которую указывает исключение."""
        if row := self.by_code.get(code_key(alias.target_article)):
            return row
        if alias.target_ean and (row := self.by_ean.get(alias.target_ean)):
            return row
        return self.by_name.get(comparable(alias.target_name))

    def suggest(self, line: OrderLine, limit: int = 5, forced: int | None = None) -> list[Suggestion]:
        """Похожие строки бланка. `forced` — строка, найденная по штрихкоду."""
        out: list[Suggestion] = []
        seen: set[int] = set()
        if forced is not None:
            out.append(Suggestion.of(self.info(forced), 100.0))
            seen.add(forced)

        if line.name and self._names:
            matches = process.extract(
                comparable(f"{line.name} {line.trait}"),
                self._names,
                scorer=fuzz.token_set_ratio,
                score_cutoff=60,
                limit=limit,
            )
            for _, score, position in matches:
                row = self._rows[position]
                if row in seen:
                    continue
                seen.add(row)
                out.append(Suggestion.of(self.info(row), float(score)))
        return out[:limit]

    def highlights(self, taken: Container[int] = ()) -> list[TargetRow]:
        """Строки прайса с пометками поставщика, которых ещё нет в заказе."""
        return [entry for entry in self.entries if entry.marks and entry.row not in taken]

    def search(self, text: str, limit: int = 30) -> list[Suggestion]:
        """Поиск по всему бланку: артикул, штрихкод, слова названия, опечатки."""
        query = normalize_text(text)
        if not query:
            return []
        code = code_key(text)
        words = query.split()
        found: list[Suggestion] = []
        for entry in self.entries:
            if score := self._score(entry, query, words, code):
                found.append(Suggestion.of(entry, score))
        found.sort(key=lambda s: (-s.score, s.row))
        return found[:limit]

    @staticmethod
    def _score(entry: TargetRow, query: str, words: Sequence[str], code: str) -> float:
        if code:
            entry_code = code_key(entry.article)
            if code in (entry_code, entry.ean):
                return 100.0
            if len(code) >= 3 and (code in entry_code or code in entry.ean):
                return 94.0
        if query in entry.blob:
            return 92.0
        tokens = entry.blob.split()
        if all(_covers(word, tokens) for word in words):
            return 88.0
        # Числа сравнивать на похожесть нельзя: у соседних штрихкодов различие
        # в одну цифру — это другой товар, а не опечатка.
        if not any(ch.isalpha() for ch in query):
            return 0.0
        # Среднее по словам запроса, а не по строке целиком: длинное название
        # бланка иначе занижает оценку короткому запросу до нуля.
        similarity = _word_similarity(words, tokens)
        return similarity * 0.8 if similarity >= 70 else 0.0


def _covers(word: str, tokens: Sequence[str]) -> bool:
    """Слово запроса найдено в строке. Короткое слово — только целиком:
    иначе «2» совпало бы с «200мл» и вернуло полбланка."""
    if len(word) >= 3:
        return any(token.startswith(word) for token in tokens)
    return word in tokens


def _word_similarity(words: Sequence[str], tokens: Sequence[str]) -> float:
    """Насколько слова запроса похожи на слова строки: среднее лучших совпадений."""
    if not (words and tokens):
        return 0.0
    return sum(max(fuzz.ratio(word, token) for token in tokens) for word in words) / len(words)


def detect_marks(values: Iterable[Any]) -> tuple[str, ...]:
    """Пометки поставщика в строке прайса: НОВИНКА, ХИТ, LIMITED и их варианты.

    Просматривается вся строка, а не одна колонка: у разных поставщиков пометка
    стоит то в отдельной колонке («NEW/ХИТ»), то прямо в названии.
    """
    found: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        for token in normalize_text(value).split():
            label = _MARK_TOKENS.get(token)
            if label and label not in found:
                found.append(label)
    return tuple(found)


def is_tester(article: str, name: str) -> bool:
    """Тестер отличается от обычной позиции суффиксом артикула или пометкой в названии."""
    if _TESTER_MARK in normalize_text(name):
        return True
    code = article.strip().lower()
    return bool(code) and code.endswith(("t", "т"))


def transfer(
    source: OrderSheet,
    target: OrderSheet,
    aliases: AliasBook | None = None,
    index: TargetIndex | None = None,
) -> list[OrderLine]:
    """Сопоставляет позиции заказа со строками бланка по артикулу и штрихкоду.

    Исключение проверяется первым: это осознанное решение пользователя, и оно
    должно перекрывать автоматический подбор, а не подстраиваться под него.
    """
    lines = read_orders(source)
    index = index or TargetIndex(target)
    for line in lines:
        if aliases and (alias := aliases.find(line)) and (row := index.locate(alias)):
            line.target_row, line.method = row, "Исключение"
            continue
        if row := index.by_code.get(code_key(line.article)):
            line.target_row, line.method = row, "Артикул"
            continue
        row = index.by_ean.get(line.ean)
        # Штрихкод тестера может совпадать с обычной позицией. Молча записать
        # заказ в строку тестера нельзя — она уходит в подсказки с пометкой.
        if row and not (row in index.testers and not is_tester(line.article, line.name)):
            line.target_row, line.method = row, "Штрихкод"
            continue
        line.suggestions = index.suggest(line, forced=row)
    return lines


def transfer_with_index(
    source: OrderSheet, target: OrderSheet, aliases: AliasBook | None = None
) -> tuple[list[OrderLine], TargetIndex]:
    """То же, что `transfer`, но отдаёт индекс — он нужен для поиска по бланку."""
    index = TargetIndex(target)
    return transfer(source, target, aliases, index), index


def build_updates(lines: Iterable[OrderLine], target: OrderSheet) -> list[tuple[int, int, Any]]:
    """Ячейки для записи: только строки с найденным соответствием."""
    if target.quantity is None:
        return []
    column = target.quantity + 1
    return [
        (line.target_row, column, _as_number(line.quantity))
        for line in lines
        if line.matched
    ]


def _as_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def summarize(lines: Sequence[OrderLine]) -> dict[str, float]:
    matched = [line for line in lines if line.matched]
    missing = [line for line in lines if not line.matched]
    return {
        "позиций": len(lines),
        "перенесено": len(matched),
        "не найдено": len(missing),
        "добавлено": sum(1 for line in lines if line.added),
        "штук перенесено": sum(line.quantity for line in matched),
        "штук потеряно": sum(line.quantity for line in missing),
    }
