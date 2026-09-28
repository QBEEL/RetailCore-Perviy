"""Счёт поставщика в PDF → книга Excel.

Поставщики присылают счёт PDF-файлом из 1С, а программа работает с Excel.
Перепечатывать тридцать строк руками — это ошибки в артикулах и количествах,
поэтому таблица снимается с PDF как есть: текстовый слой у таких файлов есть,
распознавать картинку не нужно.

Колонки определяются по линиям сетки, а не по положению заголовков. Числа в
ячейках прижаты вправо, заголовки — по центру, и «4» из «Кол-во» оказывается
ближе к заголовку «Ед.», чем к своему. Линии же у таблицы из 1С есть всегда.

Строки таблицы сверяются с «Итого» и «Всего наименований» под ней: если
разбор что-то потерял или склеил, человек узнаёт об этом сразу, а не после
оплаты.
"""
from __future__ import annotations

import os
import re
from bisect import bisect_right
from dataclasses import dataclass, field

from openpyxl import Workbook

from .sheets import head, note, row, title, widths

# Рамка страницы — прямоугольник во всю страницу. Она касается всех таблиц
# сразу и склеила бы их в одну, поэтому такие прямоугольники не считаются.
_FRAME_SHARE = 0.5
# Точность, с которой концы линий считаются сошедшимися, в пунктах.
_SNAP = 2.0

_HEADER_WORDS = ("товар", "наименование", "артикул", "код", "кол", "цена", "сумма",
                 "ед.", "количество")
_TEXT_HEADERS = re.compile(r"артикул|код|штрих|ean|barcode|товар|наименован|ед\.",
                           re.IGNORECASE)
_NUMBER = re.compile(r"^-?\d{1,3}(?:[   ]\d{3})*(?:[.,]\d+)?$|^-?\d+(?:[.,]\d+)?$")
_PARTY_LABELS = ("Поставщик", "Исполнитель", "Грузоотправитель", "Покупатель",
                 "Заказчик", "Грузополучатель", "Плательщик")
_TOTAL_LABELS = {
    "итого": "Итого",
    "в том числе ндс": "В том числе НДС",
    "сумма ндс": "Сумма НДС",
    "всего к оплате": "Всего к оплате",
}
_COUNT = re.compile(r"Всего наименований\s+(\d+)", re.IGNORECASE)
_TITLE = re.compile(r"^(сч[её]т|счет-оферта|сч[её]т-договор)\b", re.IGNORECASE)


class PdfProblem(ValueError):
    """Счёт не разобрать. Текст — для человека: что с файлом и что делать."""


@dataclass
class Invoice:
    header: list[str]
    rows: list[list[object]]
    title: str = ""
    parties: dict[str, str] = field(default_factory=dict)
    totals: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# --- чтение PDF ------------------------------------------------------------------

@dataclass
class _Line:
    text: str
    x0: float
    x1: float
    y: float
    page: int = 0


@dataclass
class _Grid:
    xs: list[float]
    ys: list[float]           # сверху вниз
    cells: dict[tuple[int, int], list[tuple[float, str]]]

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return self.xs[0], self.ys[-1], self.xs[-1], self.ys[0]

    def table(self) -> list[list[str]]:
        rows = len(self.ys) - 1
        columns = len(self.xs) - 1
        return [[_join(self.cells.get((r, c), [])) for c in range(columns)]
                for r in range(rows)]


def read_invoice(path: str) -> Invoice:
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LAParams
        from pdfminer.pdfparser import PDFSyntaxError
    except ImportError as error:  # сборка без pdfminer — сообщить, а не упасть
        raise PdfProblem(f"Чтение PDF недоступно в этой сборке: {error}") from error

    name = os.path.basename(path)
    try:
        pages = list(extract_pages(path, laparams=LAParams()))
    except (PDFSyntaxError, OSError, ValueError) as error:
        raise PdfProblem(f"{name}: не удалось открыть PDF — {error}") from error
    except Exception as error:  # noqa: BLE001 — pdfminer бросает что угодно на битом файле
        raise PdfProblem(f"{name}: PDF повреждён или защищён — {error}") from error

    grids: list[_Grid] = []
    loose: list[_Line] = []
    has_text = False
    for number, page in enumerate(pages):
        page_grids = _grids(page)
        chars, lines = _text(page, number)
        has_text = has_text or bool(chars)
        _fill(page_grids, chars)
        grids.extend(page_grids)
        boxes = [grid.bbox for grid in page_grids]
        loose.extend(line for line in lines if not any(_inside(line, box) for box in boxes))

    if not has_text:
        raise PdfProblem(
            f"{name}: в PDF нет текста — похоже, это скан или фотография. "
            "Попросите у поставщика счёт, выгруженный из 1С, а не отсканированный.")

    header, body = _items(grids)
    if not header:
        raise PdfProblem(
            f"{name}: в PDF не нашлась таблица товаров с линиями сетки. "
            "Разбираются счета, выгруженные из 1С или похожих программ.")

    invoice = Invoice(header=header, rows=_typed(header, body))
    invoice.title = next((line.text for line in loose if _TITLE.match(line.text)), "")
    invoice.parties = _parties(loose)
    invoice.totals = _totals(loose)
    invoice.warnings = _check(invoice, loose)
    return invoice


def _grids(page) -> list[_Grid]:  # type: ignore[no-untyped-def]
    """Таблицы страницы: связные группы линий, каждая со своими колонками."""
    from pdfminer.layout import LTCurve, LTLine, LTRect

    width, height = page.width, page.height
    segments: list[tuple[float, float, float, float]] = []
    for item in _walk(page):
        if not isinstance(item, (LTLine, LTRect, LTCurve)):
            continue
        x0, y0, x1, y1 = item.bbox
        # Кроме 1С, ячейки рисуют замкнутым контуром из четырёх отрезков — это
        # тот же прямоугольник, только pdfminer называет его кривой.
        boxed = isinstance(item, LTRect) or _is_box(item)
        if boxed and (x1 - x0) * (y1 - y0) > _FRAME_SHARE * width * height:
            continue
        if boxed and x1 - x0 > _SNAP and y1 - y0 > _SNAP:
            # Ячейка, нарисованная прямоугольником: берутся её стороны.
            segments += [(x0, y0, x0, y1), (x1, y0, x1, y1),
                         (x0, y0, x1, y0), (x0, y1, x1, y1)]
        elif x1 - x0 <= _SNAP or y1 - y0 <= _SNAP:
            segments.append((x0, y0, x1, y1))

    grids = []
    for group in _connected(segments):
        verticals = [(s[0] + s[2]) / 2 for s in group if s[2] - s[0] <= _SNAP < s[3] - s[1]]
        horizontals = [(s[1] + s[3]) / 2 for s in group if s[3] - s[1] <= _SNAP < s[2] - s[0]]
        xs = _cluster(verticals)
        ys = sorted(_cluster(horizontals), reverse=True)
        if len(xs) >= 3 and len(ys) >= 2:
            grids.append(_Grid(xs=xs, ys=ys, cells={}))
    return grids


def _is_box(item) -> bool:  # type: ignore[no-untyped-def]
    # Точек бывает и девять: обводку со скошенными углами Qt пишет контуром.
    points = getattr(item, "pts", None) or []
    if len(points) < 4:
        return False
    x0, y0, x1, y1 = item.bbox
    return all(min(abs(x - x0), abs(x - x1)) <= 0.5 and min(abs(y - y0), abs(y - y1)) <= 0.5
               for x, y in points)


def _connected(segments: list[tuple[float, float, float, float]]) -> list[list[tuple]]:
    parent = list(range(len(segments)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Сортировка по левому краю отсекает заведомо далёкие пары: без неё на
    # странице с восемью сотнями линий сравнений было бы за полмиллиона.
    order = sorted(range(len(segments)), key=lambda i: segments[i][0])
    for position, i in enumerate(order):
        ax0, ay0, ax1, ay1 = segments[i]
        for j in order[position + 1:]:
            bx0, by0, bx1, by1 = segments[j]
            if bx0 > ax1 + _SNAP:
                break
            if by0 <= ay1 + _SNAP and ay0 <= by1 + _SNAP:
                parent[root(i)] = root(j)
    groups: dict[int, list[tuple]] = {}
    for i, segment in enumerate(segments):
        groups.setdefault(root(i), []).append(segment)
    return list(groups.values())


def _cluster(values: list[float]) -> list[float]:
    result: list[float] = []
    for value in sorted(values):
        if result and value - result[-1] <= _SNAP:
            continue
        result.append(value)
    return result


def _walk(item):  # type: ignore[no-untyped-def]
    yield item
    if hasattr(item, "__iter__"):
        for child in item:
            yield from _walk(child)


def _text(page, number: int) -> tuple[list[tuple[float, float, str, int]], list[_Line]]:  # type: ignore[no-untyped-def]
    """Знаки с координатами центра и строки текста целиком.

    Пробелы pdfminer отдаёт без координат: они идут за предыдущим знаком, а
    строка, пересекающая несколько ячеек, всё равно делится по ним ниже.
    """
    from pdfminer.layout import LTAnno, LTChar, LTTextLine

    chars: list[tuple[float, float, str, int]] = []
    lines: list[_Line] = []
    for item in _walk(page):
        if not isinstance(item, LTTextLine):
            continue
        text = item.get_text().strip()
        if text:
            x0, y0, x1, y1 = item.bbox
            lines.append(_Line(text, x0, x1, (y0 + y1) / 2, number))
        line_id = id(item)
        for glyph in item:
            if isinstance(glyph, LTChar):
                x0, y0, x1, y1 = glyph.bbox
                chars.append(((x0 + x1) / 2, (y0 + y1) / 2, glyph.get_text(), line_id))
            elif isinstance(glyph, LTAnno) and glyph.get_text() == " " and chars:
                x, y, _, owner = chars[-1]
                chars.append((x, y, " ", owner))
    return chars, lines


def _fill(grids: list[_Grid], chars: list[tuple[float, float, str, int]]) -> None:
    """Раскладывает знаки по ячейкам. Строки текста в ячейке — сверху вниз."""
    fragments: dict[tuple[int, int, int, int], list[str]] = {}
    heights: dict[tuple[int, int, int, int], float] = {}
    for x, y, text, line_id in chars:
        for number, grid in enumerate(grids):
            left, bottom, right, top = grid.bbox
            if not (left <= x <= right and bottom <= y <= top):
                continue
            column = bisect_right(grid.xs, x) - 1
            row_index = sum(1 for edge in grid.ys[1:] if edge > y)
            key = (number, row_index, column, line_id)
            fragments.setdefault(key, []).append(text)
            heights.setdefault(key, y)
            break
    for (number, row_index, column, _), parts in fragments.items():
        text = "".join(parts).strip()
        if text:
            grids[number].cells.setdefault((row_index, column), []).append(
                (heights[(number, row_index, column, _)], text))


def _join(parts: list[tuple[float, str]]) -> str:
    """Строки одной ячейки → одна строка. Перенос по дефису не даёт пробела."""
    result = ""
    for _, text in sorted(parts, key=lambda part: -part[0]):
        text = re.sub(r"\s+", " ", text)
        result = f"{result}{text}" if result.endswith("-") or not result else f"{result} {text}"
    return result.strip()


def _inside(line: _Line, box: tuple[float, float, float, float]) -> bool:
    left, bottom, right, top = box
    return left - 1 <= line.x0 and line.x1 <= right + 1 and bottom <= line.y <= top


# --- таблица товаров -------------------------------------------------------------

def _is_header(cells: list[str]) -> bool:
    found = sum(1 for word in _HEADER_WORDS if any(word in cell.lower() for cell in cells))
    return found >= 3


def _items(grids: list[_Grid]) -> tuple[list[str], list[list[str]]]:
    """Шапка и строки таблицы товаров, включая продолжение на других страницах."""
    header: list[str] = []
    edges: list[float] = []
    body: list[list[str]] = []
    for grid in grids:
        table = grid.table()
        if not header:
            start = next((i for i, cells in enumerate(table) if _is_header(cells)), None)
            if start is None:
                continue
            header, edges = table[start], grid.xs
            body.extend(table[start + 1:])
        elif _same_columns(edges, grid.xs):
            # Продолжение на следующей странице; шапку 1С иногда повторяет.
            body.extend(cells for cells in table if cells != header)
    if not header:
        return [], []

    body = [cells for cells in body if any(cells) and not _is_numbering(cells)]
    used = [c for c in range(len(header)) if header[c] or any(r[c] for r in body)]
    return [header[c] for c in used], [[cells[c] for c in used] for cells in body]


def _same_columns(first: list[float], second: list[float]) -> bool:
    return len(first) == len(second) and all(
        abs(a - b) <= 2 * _SNAP for a, b in zip(first, second))


def _is_numbering(cells: list[str]) -> bool:
    """Строка «1 2 3 …» под шапкой — номера колонок в УПД и ТОРГ-12."""
    filled = [cell for cell in cells if cell]
    return len(filled) >= 3 and all(cell.isdigit() for cell in filled) and \
        [int(cell) for cell in filled] == list(range(int(filled[0]), int(filled[0]) + len(filled)))


def _number(text: str) -> float | None:
    text = text.strip()
    if not _NUMBER.match(text):
        return None
    return float(re.sub(r"[   ]", "", text).replace(",", "."))


def _typed(header: list[str], body: list[list[str]]) -> list[list[object]]:
    """Числа — числами, чтобы Excel считал. Артикулы остаются текстом.

    Артикул «0022» числом превратился бы в 22 и перестал находиться в прайсе,
    поэтому колонки кодов и наименований не трогаются, как и любая колонка,
    где встречается ведущий ноль.
    """
    numeric: list[bool] = []
    for column, name in enumerate(header):
        values = [cells[column] for cells in body if cells[column]]
        numeric.append(
            bool(values)
            and not _TEXT_HEADERS.search(name)
            and all(_number(value) is not None for value in values)
            and not any(re.match(r"^0\d", value) for value in values))
    rows: list[list[object]] = []
    for cells in body:
        typed: list[object] = []
        for column, value in enumerate(cells):
            if numeric[column] and value:
                number = _number(value)
                typed.append(int(number) if number is not None and number.is_integer()
                             and "," not in value and "." not in value else number)
            else:
                typed.append(value or None)
        rows.append(typed)
    return rows


# --- сведения под таблицей и над ней -------------------------------------------

def _parties(lines: list[_Line]) -> dict[str, str]:
    labels = [line for line in lines
              if line.text.rstrip(":") in _PARTY_LABELS and line.text.endswith(":")]
    result: dict[str, list[_Line]] = {}
    for line in lines:
        if line in labels:
            continue
        near = [label for label in labels if line.page == label.page
                and line.x0 > label.x1 and abs(line.y - label.y) <= 14]
        if not near:
            continue
        owner = min(near, key=lambda label: abs(line.y - label.y))
        result.setdefault(owner.text.rstrip(":"), []).append(line)
    return {name: " ".join(part.text for part in sorted(parts, key=lambda p: -p.y))
            for name, parts in result.items()}


def _totals(lines: list[_Line]) -> dict[str, float]:
    result: dict[str, float] = {}
    for line in lines:
        key = line.text.rstrip(":").strip().lower()
        if key not in _TOTAL_LABELS:
            continue
        values = sorted((other for other in lines if other.page == line.page
                         and other.x0 > line.x1 and abs(other.y - line.y) <= 2),
                        key=lambda other: other.x0)
        numbers = [n for n in (_number(v.text) for v in values) if n is not None]
        if numbers:
            # Правее всех — сумма последней колонки, её и сверяем.
            result[_TOTAL_LABELS[key]] = numbers[-1]
    return result


def _check(invoice: Invoice, lines: list[_Line]) -> list[str]:
    warnings = []
    for line in lines:
        if match := _COUNT.search(line.text):
            expected = int(match.group(1))
            if expected != len(invoice.rows):
                warnings.append(
                    f"В счёте написано «всего наименований {expected}», "
                    f"а в таблице разобрано {len(invoice.rows)}")
            break

    total = invoice.totals.get("Итого", invoice.totals.get("Всего к оплате"))
    column = _amount_column(invoice)
    if total is not None and column is not None:
        amount = round(sum(r[column] for r in invoice.rows
                           if isinstance(r[column], (int, float))), 2)
        if abs(amount - total) > 0.01:
            warnings.append(
                f"Сумма строк {_money(amount)} не сходится с «Итого» {_money(total)} — "
                "сверьте таблицу с PDF")
    return warnings


def _amount_column(invoice: Invoice) -> int | None:
    """Последняя денежная колонка — та, что в «Итого» стоит правее всех."""
    for column in range(len(invoice.header) - 1, -1, -1):
        if invoice.header[column].lower().startswith("сумма") and any(
                isinstance(r[column], (int, float)) for r in invoice.rows):
            return column
    return None


def _money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


# --- запись Excel -----------------------------------------------------------------

def excel_path_for(pdf_path: str) -> str:
    return os.path.splitext(pdf_path)[0] + ".xlsx"


def save(invoice: Invoice, destination: str, source: str = "") -> str:
    """Лист «Счёт» — таблица товаров с шапкой в первой строке.

    Шапка именно в первой строке, без заголовков над ней: книгу сразу
    открывают в «Заказе» и «Сопоставлении», и колонки там находятся сами.
    Реквизиты и итоги — на втором листе.
    """
    book = Workbook()
    sheet = book.active
    sheet.title = "Счёт"
    formats = "".join(_format(invoice, column) for column in range(len(invoice.header)))
    head(sheet, 1, invoice.header)
    for number, values in enumerate(invoice.rows):
        row(sheet, number + 2, values, formats=formats, stripe=number % 2 == 1)
    widths(sheet, 1)

    info = book.create_sheet("Документ")
    line = title(info, 1, invoice.title or "Счёт", 2)
    if source:
        line = note(info, line, f"Переведено из {os.path.basename(source)}", 2)
    for message in invoice.warnings:
        line = note(info, line, f"⚠ {message}", 2)
    line += 1
    pairs: list[tuple[str, object]] = list(invoice.parties.items())
    pairs += [("Наименований", len(invoice.rows))]
    pairs += list(invoice.totals.items())
    line = head(info, line, ["Реквизит", "Значение"])
    info.freeze_panes = None
    for number, (name, value) in enumerate(pairs):
        row(info, line + number, [name, value],
            formats="tm" if isinstance(value, float) else "tt")
    info.column_dimensions["A"].width = 22
    info.column_dimensions["B"].width = 110
    book.save(destination)
    return destination


def _format(invoice: Invoice, column: int) -> str:
    values = [r[column] for r in invoice.rows if r[column] is not None]
    if not values or not all(isinstance(v, (int, float)) for v in values):
        return "t"
    return "n" if all(isinstance(v, int) for v in values) else "m"


def convert(pdf_path: str, destination: str | None = None) -> tuple[str, Invoice]:
    """PDF → xlsx рядом с ним (или по указанному пути). Возвращает путь и разбор."""
    invoice = read_invoice(pdf_path)
    target = destination or excel_path_for(pdf_path)
    try:
        save(invoice, target, pdf_path)
    except PermissionError as error:
        raise PdfProblem(
            f"Не удалось записать {os.path.basename(target)}: файл открыт в Excel? "
            "Закройте его и попробуйте снова.") from error
    return target, invoice
