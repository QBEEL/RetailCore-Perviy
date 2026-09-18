"""Выгрузка кодов маркировки в 1С: номенклатура, сопоставление, файл загрузки.

Последний шаг приёмки. Коды из УПД сверены с товаром, и теперь их нужно
завести в 1С штрихкодами номенклатуры. Обработка загрузки ждёт три колонки —
код номенклатуры, характеристику и сам код, — а в УПД ничего этого нет:
поставщик пишет своё название и свой GTIN. Чем одно связано с другим, знает
только выгрузка номенклатуры из 1С, и её приходится просить у пользователя.

Сопоставление идёт по убыванию надёжности, и каждый способ виден в таблице —
чтобы не пришлось гадать, почему строка встала именно на эту номенклатуру:

1. **Ручная привязка.** Однажды указанное запоминается по GTIN и больше не
   спрашивается: товар тот же самый, и второй раз выбирать его незачем.
2. **Штрихкод.** Если в выгрузке есть колонка штрихкодов, GTIN из УПД ищется в
   ней. Это единственный способ, который не зависит от того, как поставщик
   назвал товар, поэтому он первый.
3. **Название.** 1С дописывает к названию единицу измерения («…, шт»), и после
   её отбрасывания названия у поставщика и в базе совпадают слово в слово —
   на живой поставке так сошлись все сорок две позиции.
4. **Похожие.** Если точного совпадения нет, показываются ближайшие, но сами
   собой они не применяются: молча подставить не ту номенклатуру значит завести
   коды чужому товару, а заметить это потом почти невозможно.

Строка без номенклатуры в файл не попадает, и сколько таких — говорится вслух.
Загрузить половину кодов, не сказав об этом, хуже, чем не загрузить ничего.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, Sequence

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from rapidfuzz import fuzz, process

from .. import workbook as workbook_module
from ..normalize import digits_only, normalize_text
from .reconcile import Reconciliation, key_of
from .upd import Line

# Колонки шаблона загрузки — дословно, как их ждёт обработка в 1С.
COLUMNS = ("Код номенклатуры", "Характеристика", "ЗначениеШтрихкода")

# Как названы колонки в выгрузке номенклатуры. Выгружают её по-разному, и
# требовать одного написания значит требовать переименовать шапку руками.
NAME_TITLES = ("номенклатура", "наименование", "товар", "название")
CODE_TITLES = ("код номенклатуры", "код")
FEATURE_TITLES = ("характеристика", "характеристика номенклатуры")
BARCODE_TITLES = ("штрихкод", "штрих код", "штрих-код", "ean", "gtin", "штрихкоды")

# Единица измерения, которую 1С дописывает к названию номенклатуры. Отбрасывается
# с конца названия: в середине «шт» — часть названия, а не единица.
UNIT_TAIL = ("шт", "штук", "штука", "уп", "упак", "пар", "пара")

# Сколько строк просматривать в поисках шапки.
HEADER_LIMIT = 15

# Ниже этого сходства кандидат не показывается вовсе: «похоже на 30 %» — это не
# подсказка, а повод выбрать не то.
SIMILAR_FLOOR = 62

# Сколько похожих показывать в окне выбора сверх найденных поиском.
SIMILAR_LIMIT = 8


class OnecProblem(Exception):
    """Выгрузка номенклатуры не читается. Текст пригоден для показа."""


@dataclass(slots=True)
class Item:
    """Строка выгрузки номенклатуры из 1С."""

    name: str = ""
    code: str = ""
    feature: str = ""
    barcode: str = ""
    row: int = 0

    @property
    def title(self) -> str:
        return f"{self.name} · {self.code}" if self.code else self.name

    @property
    def key(self) -> str:
        """Чем номенклатура опознаётся в файле загрузки: код и характеристика."""
        return f"{self.code}|{self.feature}"

    @property
    def short(self) -> str:
        """Как номенклатура показывается в строке таблицы."""
        return f"{self.code} · {self.feature}" if self.feature else self.code


@dataclass(slots=True)
class Catalog:
    """Номенклатура 1С, готовая к поиску."""

    items: list[Item] = field(default_factory=list)
    path: str = ""
    # Ключи названий считаются один раз при чтении: сопоставление обходит
    # номенклатуру на каждой строке документа, и пересчёт ключей на каждом
    # обходе превратил бы поиск по большой базе в заметную паузу.
    keys: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.keys:
            self.keys = [name_key(item.name) for item in self.items]

    @property
    def has_barcodes(self) -> bool:
        return any(item.barcode for item in self.items)

    @property
    def summary(self) -> str:
        parts = [f"позиций: {len(self.items)}"]
        if self.has_barcodes:
            parts.append("есть штрихкоды — сопоставление пойдёт по ним")
        else:
            parts.append("штрихкодов нет — сопоставление по названию")
        if self.path:
            parts.append(os.path.basename(self.path))
        return " · ".join(parts)

    def by_barcode(self, barcode: str) -> Item | None:
        wanted = digits_only(barcode).lstrip("0")
        if not wanted:
            return None
        for item in self.items:
            if digits_only(item.barcode).lstrip("0") == wanted:
                return item
        return None

    def by_name(self, name: str) -> list[Item]:
        wanted = name_key(name)
        if not wanted:
            return []
        return [item for item, key in zip(self.items, self.keys) if key == wanted]

    def by_key(self, key: str) -> Item | None:
        return next((item for item in self.items if item.key == key), None)

    def similar(self, name: str, limit: int = SIMILAR_LIMIT) -> list[Item]:
        """Ближайшие по названию — для показа человеку, не для подстановки."""
        wanted = name_key(name)
        if not wanted or not self.items:
            return []
        found = process.extract(wanted, self.keys, scorer=fuzz.token_set_ratio,
                                limit=limit)
        return [self.items[index] for _, score, index in found
                if score >= SIMILAR_FLOOR]


def name_key(name: str) -> str:
    """Название, приведённое к виду, в котором его можно сравнивать.

    Единица измерения отбрасывается с конца: в 1С номенклатура называется
    «Бюстгальтер АТЛ-Эрика, нимфа (M нимфа), шт», а в УПД — тем же самым без
    последних двух знаков, и без этого шага не совпала бы ни одна позиция.
    """
    words = normalize_text(name).split()
    while words and words[-1] in UNIT_TAIL:
        words.pop()
    return " ".join(words)


# --- чтение выгрузки ---------------------------------------------------------------

def read_catalog(path: str, sheet: str | None = None) -> Catalog:
    """Читает выгрузку номенклатуры из 1С."""
    if not path or not os.path.exists(path):
        raise OnecProblem("Файл не найден")
    try:
        rows, _ = workbook_module.read_raw(path, sheet)
    except ValueError as error:
        raise OnecProblem(str(error)) from error
    if not rows:
        raise OnecProblem("В файле нет ни одной строки")

    header_index, columns = _header(rows)
    if header_index is None:
        raise OnecProblem(
            "Не нашлась шапка с колонками «Номенклатура» и «Код номенклатуры». "
            "Нужна выгрузка номенклатуры из 1С — название, код, характеристика "
            "и, если есть, штрихкод")

    items: list[Item] = []
    for number, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        name = _cell(row, columns.get("name"))
        code = _cell(row, columns.get("code"))
        if not name and not code:
            continue
        items.append(Item(
            name=name,
            code=code,
            feature=_cell(row, columns.get("feature")),
            barcode=_cell(row, columns.get("barcode")),
            row=number,
        ))
    if not items:
        raise OnecProblem("Шапка нашлась, но строк номенклатуры под ней нет")
    return Catalog(items=items, path=path)


def _header(rows: Sequence[Sequence[object]]) -> tuple[int | None, dict[str, int]]:
    """Где шапка и какая колонка за что отвечает."""
    for index, row in enumerate(rows[:HEADER_LIMIT]):
        columns: dict[str, int] = {}
        for column, value in enumerate(row):
            title = normalize_text(value)
            if not title:
                continue
            for role, titles in (("name", NAME_TITLES), ("code", CODE_TITLES),
                                 ("feature", FEATURE_TITLES),
                                 ("barcode", BARCODE_TITLES)):
                if role not in columns and title in titles:
                    columns[role] = column
                    break
        # Код номенклатуры — то, ради чего файл и нужен; без него строки
        # загрузки не собрать, как бы ни назывались остальные колонки.
        if "name" in columns and "code" in columns:
            return index, columns
    return None, {}


def _cell(row: Sequence[object], column: int | None) -> str:
    if column is None or column >= len(row):
        return ""
    value = row[column]
    if value is None:
        return ""
    text = str(value).strip()
    # Код номенклатуры «00-00127672» приходит строкой, а вот штрихкод Excel
    # успевает сделать числом, и тогда он выглядит как «4680962313750.0».
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


# --- сопоставление -------------------------------------------------------------------

class MatchKind(Enum):
    """Как строка нашла свою номенклатуру. Показывается в таблице."""

    MANUAL = ("manual", "Привязано вручную")
    BARCODE = ("barcode", "По штрихкоду")
    NAME = ("name", "По названию")
    SIMILAR = ("similar", "Похоже — подтвердите")
    NONE = ("none", "Не найдено")

    def __init__(self, value: str, title: str) -> None:
        self._value_ = value
        self.title = title

    @property
    def ready(self) -> bool:
        """Можно ли выгружать эту строку без вопросов к человеку."""
        return self in (MatchKind.MANUAL, MatchKind.BARCODE, MatchKind.NAME)


@dataclass(slots=True)
class LineMatch:
    """Строка УПД и найденная ей номенклатура 1С."""

    line: Line
    item: Item | None = None
    kind: MatchKind = MatchKind.NONE
    candidates: list[Item] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.item is not None and self.kind.ready

    @property
    def state(self) -> str:
        if self.ready:
            return self.kind.title
        if self.candidates:
            return f"{MatchKind.SIMILAR.title}: {self.candidates[0].name}"
        return MatchKind.NONE.title


def match(lines: Iterable[Line], catalog: Catalog,
          links: Mapping[str, Mapping[str, str]] | None = None) -> list[LineMatch]:
    """Связывает строки УПД с номенклатурой 1С."""
    saved = dict(links or {})
    found: list[LineMatch] = []
    for line in lines:
        found.append(_match_line(line, catalog, saved))
    return found


def _match_line(line: Line, catalog: Catalog,
                links: Mapping[str, Mapping[str, str]]) -> LineMatch:
    if (link := links.get(gtin_key(line.gtin))) is not None:
        code = str(link.get("code") or "")
        feature = str(link.get("feature") or "")
        item = catalog.by_key(f"{code}|{feature}")
        # Привязка могла указывать на номенклатуру, которой в этой выгрузке
        # нет. Своя копия вместо отказа: код и характеристика — это и есть всё,
        # что нужно файлу загрузки, а название здесь только для показа.
        item = item or Item(name=str(link.get("name") or line.name), code=code,
                            feature=feature)
        if item.code:
            return LineMatch(line=line, item=item, kind=MatchKind.MANUAL)

    if catalog.has_barcodes and (item := catalog.by_barcode(line.gtin)) is not None:
        return LineMatch(line=line, item=item, kind=MatchKind.BARCODE)

    exact = catalog.by_name(line.name)
    if len(exact) == 1:
        return LineMatch(line=line, item=exact[0], kind=MatchKind.NAME)
    if len(exact) > 1:
        # Одно название на несколько номенклатур — выбирать за пользователя
        # нельзя: различаются они чем-то, чего в УПД нет.
        return LineMatch(line=line, kind=MatchKind.SIMILAR, candidates=exact)

    similar = catalog.similar(line.name)
    if similar:
        return LineMatch(line=line, kind=MatchKind.SIMILAR, candidates=similar)
    return LineMatch(line=line, kind=MatchKind.NONE)


def gtin_key(gtin: str) -> str:
    """Ключ ручной привязки. Ведущие нули у GTIN-14 и EAN-13 не различают товар."""
    return digits_only(gtin).lstrip("0")


def summarize(matches: Sequence[LineMatch]) -> str:
    """Одна строка о том, что получилось сопоставить."""
    if not matches:
        return "Выгрузка номенклатуры не загружена."
    ready = sum(1 for item in matches if item.ready)
    parts = [f"сопоставлено {ready} из {len(matches)}"]
    if waiting := sum(1 for item in matches
                      if not item.ready and item.candidates):
        parts.append(f"ждут подтверждения {waiting}")
    if missing := sum(1 for item in matches
                      if not item.ready and not item.candidates):
        parts.append(f"не найдено {missing}")
    if ready == len(matches):
        parts.append("можно выгружать")
    return " · ".join(parts)


# --- файл для 1С ----------------------------------------------------------------------

@dataclass(slots=True)
class Export:
    """Что ушло в файл и что в него не попало."""

    path: str = ""
    rows: int = 0
    skipped_lines: list[Line] = field(default_factory=list)
    skipped_codes: int = 0

    @property
    def clean(self) -> bool:
        return not self.skipped_lines

    @property
    def summary(self) -> str:
        parts = [f"строк в файле: {self.rows}"]
        if self.skipped_lines:
            parts.append(
                f"не попало кодов: {self.skipped_codes} "
                f"по {len(self.skipped_lines)} позициям без номенклатуры")
        return " · ".join(parts)


def build_rows(matches: Sequence[LineMatch], session: Reconciliation,
               only_scanned: bool = True) -> tuple[list[tuple[str, str, str]], Export]:
    """Строки файла загрузки: по одной на каждый код.

    По умолчанию берутся только сверенные коды. Код из документа, которого не
    нашлось на товаре, — это вещь, которая не приехала, и заводить ей штрихкод
    значит поставить в 1С на приход то, чего нет.
    """
    rows: list[tuple[str, str, str]] = []
    report = Export()
    ready = {item.line.number: item.item for item in matches if item.ready}
    skipped: dict[str, Line] = {}
    for line, mark in session.document.marks:
        if only_scanned and key_of(mark.value) not in session.seen:
            continue
        item = ready.get(line.number)
        if item is None:
            skipped[line.number] = line
            report.skipped_codes += 1
            continue
        rows.append((item.code, item.feature, mark.value))
    report.rows = len(rows)
    report.skipped_lines = list(skipped.values())
    return rows, report


def save(matches: Sequence[LineMatch], session: Reconciliation, destination: str,
         only_scanned: bool = True) -> Export:
    """Пишет файл загрузки.

    Только xlsx, и это не лень. Код маркировки — строка из набора GS1, а в нём
    есть и точка с запятой, и кавычка: в живой поставке встретились обе. В csv
    такую строку приходится экранировать, и достаточно одной обработки,
    читающей файл простым разделением по «;», чтобы половина кодов приехала в
    1С обрезанной — молча и без единой ошибки при загрузке.
    """
    if not destination:
        raise ValueError("Не указан файл для сохранения")
    rows, report = build_rows(matches, session, only_scanned)
    if not rows:
        raise OnecProblem(
            "Выгружать нечего: ни один код не сверен и не сопоставлен с "
            "номенклатурой 1С")
    if directory := os.path.dirname(destination):
        os.makedirs(directory, exist_ok=True)
    _save_xlsx(rows, destination)
    report.path = destination
    return report


def default_name(session: Reconciliation, folder: str) -> str:
    """Имя по умолчанию: «Загрузка в 1С — УПД 3349.xlsx»."""
    number = session.document.number or "без номера"
    name = "".join(" " if char in '\\/:*?"<>|' else char
                   for char in f"Загрузка в 1С — УПД {number}")
    return os.path.join(folder, f"{' '.join(name.split())}.xlsx")


def _save_xlsx(rows: Sequence[tuple[str, str, str]], destination: str) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = "Лист_1"
    for column, title in enumerate(COLUMNS, start=1):
        cell = sheet.cell(row=1, column=column, value=title)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F3864")
    for index, row in enumerate(rows, start=2):
        for column, value in enumerate(row, start=1):
            cell = sheet.cell(row=index, column=column, value=value)
            # Текстовый формат обязателен: код начинается с «01», и числом
            # Excel съел бы ведущий ноль вместе с годностью кода.
            cell.number_format = "@"
    for column, width in enumerate((22, 18, 40), start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width
    sheet.freeze_panes = "A2"
    book.save(destination)
