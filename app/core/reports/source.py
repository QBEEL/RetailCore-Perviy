"""Чтение исходных выгрузок продаж и приведение их к `SaleRow`.

Выгрузка 1С приходит не таблицей: сверху лежат «Параметры:» и «Отбор:», шапка
начинается строкой шестой, часть колонок объединена и потому пуста, а снизу
приписано «Итого». Здесь всё это распознаётся само — пользователю остаётся
подтвердить разметку, если автоматика ошиблась.

Разметка колонок хранится не в профиле отчёта, а рядом с файлом-источником:
формат отчёта у поставщика один, а выгрузки менеджеры делают в разных разрезах,
и общий профиль не должен ломаться из-за чужой колонки.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..workbook import list_sheets, open_workbook
from .models import Field, SaleRow

# Сколько строк сверху просматривается в поисках шапки. Выгрузка 1С кладёт её
# на шестую; запас взят на случай более многословных параметров отбора.
HEADER_SEARCH_DEPTH = 30

# Заголовок опознаётся по подстрокам в нижнем регистре. Порядок внутри роли
# важен: более длинный вариант проверяется раньше, иначе «сумма» перехватила бы
# «сумма авт.».
_PATTERNS: tuple[tuple[Field, tuple[str, ...]], ...] = (
    (Field.STORE, ("магазин", "подразделение", "торговая точка", "точка продаж")),
    (Field.ITEM, ("номенклатура с характеристикой", "номенклатура", "товар",
                  "наименование")),
    (Field.CATEGORY, ("категория товара", "категория", "товарная группа")),
    (Field.BRAND, ("бренд", "торговая марка", "производитель")),
    (Field.PROMO, ("акция", "промо", "маркетинговое мероприятие")),
    (Field.PERCENT, ("% авт", "% скидки", "процент скидки", "размер скидки")),
    (Field.YEAR, ("год",)),
    (Field.MONTH, ("месяц",)),
    (Field.QTY, ("количество", "кол-во", "шт")),
    (Field.AUTO_DISCOUNT, ("сумма авт", "автоматическая скидка", "сумма скидки",
                           "скидка авт")),
    (Field.BONUS_DISCOUNT, ("скидка бонусами", "оплачено бонусами", "бонусная скидка")),
    (Field.BONUS_ACCRUED, ("начислено бонусов", "начисленные бонусы")),
    (Field.REVENUE, ("сумма продаж", "выручка", "сумма")),
)

# Слово в первой колонке, которым 1С помечает итоговую строку. В отчёт она бы
# приехала товаром с названием «Итого» и удвоила бы все цифры.
_TOTAL_MARKERS = ("итого", "всего", "итог")

_NUMBER = re.compile(r"[^\d,.\-]")


@dataclass(slots=True)
class ColumnMap:
    """Какая колонка листа за какую роль отвечает. Индексы с нуля."""

    columns: dict[Field, int] = field(default_factory=dict)
    header_row: int = 0
    sheet: str = ""

    def column_of(self, role: Field) -> int:
        return self.columns.get(role, -1)

    def has(self, role: Field) -> bool:
        return self.columns.get(role, -1) >= 0

    @property
    def usable(self) -> bool:
        """Минимум для отчёта: что продано и сколько.

        Без номенклатуры строить нечего, без количества и сумм нечего считать.
        Остальное — разрезы, и их отсутствие лишь обедняет отчёт.
        """
        has_metric = any(self.has(role) for role in
                         (Field.QTY, Field.AUTO_DISCOUNT, Field.REVENUE))
        return self.has(Field.ITEM) and has_metric

    @property
    def missing(self) -> list[Field]:
        return [role for role in (Field.ITEM, Field.QTY) if not self.has(role)]

    def as_dict(self) -> dict[str, int]:
        return {role.value: index for role, index in sorted(
            self.columns.items(), key=lambda pair: pair[1])}


@dataclass(slots=True)
class SourceFile:
    """Разобранный исходник: строки продаж и то, как их прочитали."""

    path: str
    sheet: str = ""
    mapping: ColumnMap = field(default_factory=ColumnMap)
    rows: list[SaleRow] = field(default_factory=list)
    skipped: int = 0
    store_hint: str = ""

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def stores(self) -> list[str]:
        return sorted({row.store for row in self.rows if row.store})


def detect_mapping(grid: Sequence[Sequence[Any]], sheet: str = "") -> ColumnMap:
    """Ищет строку шапки и раскладывает её заголовки по ролям.

    Побеждает строка, в которой распознано больше всего ролей: у выгрузки 1С
    выше шапки лежат «Параметры:» и «Отбор:», а в них попадаются те же слова
    («Подразделение: Уссурийск…»), и брать первую подходящую строку нельзя.
    """
    best = ColumnMap(sheet=sheet)
    best_score = 0
    for index, row in enumerate(grid[:HEADER_SEARCH_DEPTH]):
        columns = _match_row(row)
        # Роли-разрезы весомее числовых: строка «Параметры» может случайно дать
        # «год» и «месяц», но никогда не даст «номенклатуру» вместе с ними.
        score = len(columns) + (3 if Field.ITEM in columns else 0)
        if score > best_score:
            best, best_score = ColumnMap(columns, index, sheet), score
    return best


def _match_row(row: Sequence[Any]) -> dict[Field, int]:
    taken: dict[Field, int] = {}
    for index, value in enumerate(row):
        title = str(value or "").strip().lower()
        if not title:
            continue
        if (role := _role_of(title)) and role not in taken:
            taken[role] = index
    return taken


def _role_of(title: str) -> Field | None:
    """Роль по заголовку. Сначала точное совпадение, потом вхождение.

    Без точного прохода «Сумма» досталась бы роли AUTO_DISCOUNT: шаблон
    «сумма скидки» её не ловит, зато «сумма» из REVENUE стоит последним, и
    порядок решал бы дело случайно.
    """
    for role, patterns in _PATTERNS:
        if title in patterns:
            return role
    for role, patterns in _PATTERNS:
        if any(pattern in title for pattern in patterns):
            return role
    return None


def read_source(
    path: str,
    *,
    sheet: str | None = None,
    mapping: ColumnMap | None = None,
    store_hint: str = "",
) -> SourceFile:
    """Читает файл продаж целиком.

    `store_hint` подставляется магазином там, где в файле нет такой колонки:
    менеджеры присылают выгрузку по одному магазину, и его название остаётся
    только в имени файла.
    """
    grid, sheet_name = _grid(path, sheet)
    known = mapping or detect_mapping(grid, sheet_name)
    source = SourceFile(path=path, sheet=sheet_name, mapping=known,
                        store_hint=store_hint)
    if not known.usable:
        return source

    name = os.path.basename(path)
    fallback = store_hint or (store_from_name(path) if not known.has(Field.STORE) else "")
    for row in grid[known.header_row + 1:]:
        if _is_total(row, known):
            continue
        sale = _sale(row, known, fallback, name)
        if sale is None:
            source.skipped += 1
            continue
        source.rows.append(sale)
    return source


def _grid(path: str, sheet: str | None) -> tuple[list[list[Any]], str]:
    """Лист целиком. `read_raw` из workbook здесь не подходит: он кеширует
    результат по файлу, а отчётность читает те же файлы после дозаписи."""
    workbook = open_workbook(path, read_only=True, data_only=True)
    try:
        names = workbook.sheetnames
        worksheet = workbook[sheet] if sheet in names else workbook.worksheets[0]
        rows = [list(row) for row in worksheet.iter_rows(values_only=True)]
        return rows, worksheet.title
    finally:
        workbook.close()


def _is_total(row: Sequence[Any], mapping: ColumnMap) -> bool:
    """Строка «Итого» в конце выгрузки.

    Проверяется первая непустая ячейка, а не колонка магазина: 1С ставит слово
    в самую левую колонку блока, и какая это будет — зависит от разреза.
    """
    for value in row:
        text = str(value or "").strip().lower().rstrip(":")
        if not text:
            continue
        return text in _TOTAL_MARKERS
    return False


def _sale(row: Sequence[Any], mapping: ColumnMap, fallback_store: str,
          source_file: str) -> SaleRow | None:
    item = _text(row, mapping.column_of(Field.ITEM))
    if not item:
        return None
    store = _text(row, mapping.column_of(Field.STORE)) or fallback_store
    sale = SaleRow(
        store=store,
        item=item,
        category=_text(row, mapping.column_of(Field.CATEGORY)),
        brand=_text(row, mapping.column_of(Field.BRAND)),
        promo=_text(row, mapping.column_of(Field.PROMO)),
        percent=_text(row, mapping.column_of(Field.PERCENT)),
        year=_int(row, mapping.column_of(Field.YEAR)),
        month=_int(row, mapping.column_of(Field.MONTH)),
        qty=_number(row, mapping.column_of(Field.QTY)),
        auto_discount=_number(row, mapping.column_of(Field.AUTO_DISCOUNT)),
        bonus_discount=_number(row, mapping.column_of(Field.BONUS_DISCOUNT)),
        revenue=_number(row, mapping.column_of(Field.REVENUE)),
        bonus_accrued=_number(row, mapping.column_of(Field.BONUS_ACCRUED)),
        source_store=store,
        source_file=source_file,
    )
    return sale


def _text(row: Sequence[Any], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _int(row: Sequence[Any], index: int) -> int:
    value = _number(row, index)
    return int(value) if value else 0


def _number(row: Sequence[Any], index: int) -> float:
    """Число из ячейки. Текст с пробелами-разделителями тоже считается числом:
    1С выгружает «1 049,99» строкой чаще, чем хотелось бы."""
    if index < 0 or index >= len(row):
        return 0.0
    value = row[index]
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = _NUMBER.sub("", str(value)).replace(",", ".")
    if not text or text in ("-", ".", "-."):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def store_from_name(path: str) -> str:
    """Магазин из имени файла — для точечных выгрузок без колонки «Магазин».

    Отбрасываются период и служебные слова: «Продажи Уссурийск июнь 2026.xlsx»
    должен дать «Уссурийск», а не всё имя целиком.
    """
    name = os.path.splitext(os.path.basename(path))[0]
    noise = {"продажи", "отчет", "отчёт", "выгрузка", "акции", "акция", "по",
             "за", "магазин", "смартбьюти", "отчетность", "отчётность"}
    months = {"январь", "февраль", "март", "апрель", "май", "июнь", "июль",
              "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
              "января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"}
    words = [w for w in re.split(r"[\s_\-]+", name) if w]
    kept = [w for w in words
            if w.lower() not in noise and w.lower() not in months
            and not w.isdigit()]
    return " ".join(kept).strip()


def sheets_of(path: str) -> list[str]:
    return list_sheets(path)


def collect(paths: Iterable[str], *, store_hints: dict[str, str] | None = None,
            mappings: dict[str, ColumnMap] | None = None) -> list[SourceFile]:
    """Читает пачку файлов. Ошибка одного не отменяет остальные —
    менеджер увидит, какой именно файл не разобрался, и заменит его."""
    hints = store_hints or {}
    known = mappings or {}
    result: list[SourceFile] = []
    for path in paths:
        try:
            result.append(read_source(path, mapping=known.get(path),
                                      store_hint=hints.get(path, "")))
        except (ValueError, OSError) as error:
            broken = SourceFile(path=path)
            broken.skipped = -1
            broken.sheet = str(error)
            result.append(broken)
    return result
