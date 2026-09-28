"""Тесты перевода счёта PDF в Excel.

Настоящие счета лежат в папках с данными, которые в репозиторий не попадают,
поэтому PDF собирается здесь же: линии сетки и текст там, где их рисует 1С.
Ширина каждого знака задана явно — полпункта на пункт кегля, — так что
положение текста в ячейке известно точно.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import pdf_invoice, workbook  # noqa: E402
from app.core.models import FieldRole  # noqa: E402

_SIZE = 8
_CHAR = _SIZE * 0.5


# --- сборка PDF ------------------------------------------------------------------

def _encode(text: str) -> bytes:
    """Кириллица — байтами cp1251; шрифт переводит их в Юникод через /Differences."""
    return text.encode("cp1251")


def _font() -> bytes:
    # Имя шрифта нестандартное: у Helvetica pdfminer берёт ширины из своих
    # метрик, где кириллицы нет, и буквы слипаются в одну точку.
    names = " ".join(f"/uni{0x410 + code - 192:04X}" for code in range(192, 256))
    return (b"<< /Type /Font /Subtype /Type1 /BaseFont /InvoiceTest "
            b"/FirstChar 32 /LastChar 255 /Widths [" + b" 500" * 224 + b"] "
            b"/Encoding << /Type /Encoding /Differences [168 /uni0401 184 /uni0451 "
            b"185 /uni2116 192 " + names.encode() + b"] >> >>")


def _page(lines: list[tuple[float, float, float, float]],
          texts: list[tuple[float, float, str]]) -> bytes:
    body = [b"0.5 w"]
    for x0, y0, x1, y1 in lines:
        body.append(f"{x0} {y0} m {x1} {y1} l S".encode())
    for x, y, text in texts:
        escaped = _encode(text).replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        body.append(f"BT /F1 {_SIZE} Tf {x} {y} Td (".encode() + escaped + b") Tj ET")
    return b"\n".join(body)


def _pdf(path: Path, pages: list[bytes]) -> Path:
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", _font()]
    kids = []
    for content in pages:
        page_number = len(objects) + 1
        kids.append(f"{page_number} 0 R")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_number + 1} 0 R >>".encode())
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content
                       + b"\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    path.write_bytes(bytes(out))
    return path


# Колонки как в счёте 1С: №, Артикул, Товары, Кол-во, Ед., Цена, Сумма.
_EDGES = [30, 58, 113, 260, 300, 330, 400, 480]
_HEADER = ["№", "Артикул", "Товары", "Кол-во", "Ед.", "Цена", "Сумма"]
_RIGHT = {0, 3, 5, 6}   # числа у 1С прижаты вправо, к линии соседней колонки


def _table(rows: list[list[str | list[str]]], top: float = 700,
           height: float = 20) -> tuple[list, list]:
    """Сетка и текст таблицы. Ячейка-список — перенос по строкам."""
    lines: list[tuple[float, float, float, float]] = []
    texts: list[tuple[float, float, str]] = []
    bottom = top - height * len(rows)
    for x in _EDGES:
        lines.append((x, bottom, x, top))
    for index in range(len(rows) + 1):
        y = top - height * index
        lines.append((_EDGES[0], y, _EDGES[-1], y))
    for index, cells in enumerate(rows):
        y_top = top - height * index
        for column, cell in enumerate(cells):
            parts = cell if isinstance(cell, list) else [cell]
            for line, text in enumerate(parts):
                y = y_top - 8 - line * (_SIZE + 1)
                if column in _RIGHT:
                    x = _EDGES[column + 1] - 2 - len(text) * _CHAR
                else:
                    x = _EDGES[column] + 2
                texts.append((x, y, text))
    return lines, texts


def _invoice(tmp_path: Path, rows: list, below: list[tuple[float, float, str]] = ()) -> Path:
    lines, texts = _table([_HEADER] + rows)
    return _pdf(tmp_path / "счёт.pdf", [_page(lines, texts + list(below))])


_ROWS = [
    ["1", "0022", ["Крем для лица", "50мл Janssen"], "3", "шт", "1 586,00", "4 758,00"],
    ["2", "1117", "Тоник 200мл", "12", "шт", "2 440,50", "29 286,00"],
]


# --- таблица ---------------------------------------------------------------------

def test_reads_columns_by_grid_lines(tmp_path: Path) -> None:
    """Число, прижатое к правой линии, остаётся в своей колонке."""
    invoice = pdf_invoice.read_invoice(str(_invoice(tmp_path, _ROWS)))

    assert invoice.header == _HEADER
    assert invoice.rows == [
        [1, "0022", "Крем для лица 50мл Janssen", 3, "шт", 1586.0, 4758.0],
        [2, "1117", "Тоник 200мл", 12, "шт", 2440.5, 29286.0],
    ]


def test_article_keeps_leading_zero(tmp_path: Path) -> None:
    invoice = pdf_invoice.read_invoice(str(_invoice(tmp_path, _ROWS)))
    assert [row[1] for row in invoice.rows] == ["0022", "1117"]


def test_table_continues_on_next_page(tmp_path: Path) -> None:
    """Продолжение без шапки и с повторённой шапкой — одна таблица."""
    first = _table([_HEADER] + _ROWS[:1])
    second = _table([_ROWS[1]], top=800)
    third = _table([_HEADER, ["3", "5000", "Маска", "1", "шт", "100,00", "100,00"]], top=800)
    path = _pdf(tmp_path / "счёт.pdf", [_page(*first), _page(*second), _page(*third)])

    invoice = pdf_invoice.read_invoice(str(path))

    assert [row[0] for row in invoice.rows] == [1, 2, 3]


def test_column_numbers_row_is_skipped(tmp_path: Path) -> None:
    """Строка «1 2 3 …» под шапкой УПД — не товар."""
    numbering = [str(n) for n in range(1, 8)]
    invoice = pdf_invoice.read_invoice(str(_invoice(tmp_path, [numbering] + _ROWS)))
    assert len(invoice.rows) == 2


# --- сверка с итогами -------------------------------------------------------------

_BELOW_OK = [(330, 620, "Итого:"), (420, 620, "34 044,00"),
             (30, 600, "Всего наименований 2, на сумму 34 044,00 руб.")]


def test_totals_and_title_are_read(tmp_path: Path) -> None:
    below = _BELOW_OK + [(30, 760, "Счет на оплату № 15 от 1 октября 2026 г."),
                         (30, 740, "Поставщик:"), (113, 740, "ООО Ромашка, ИНН 123")]
    invoice = pdf_invoice.read_invoice(str(_invoice(tmp_path, _ROWS, below)))

    assert invoice.title == "Счет на оплату № 15 от 1 октября 2026 г."
    assert invoice.parties == {"Поставщик": "ООО Ромашка, ИНН 123"}
    assert invoice.totals == {"Итого": 34044.0}
    assert invoice.warnings == []


def test_warns_when_rows_do_not_add_up(tmp_path: Path) -> None:
    below = [(330, 620, "Итого:"), (420, 620, "40 000,00"),
             (30, 600, "Всего наименований 3, на сумму 40 000,00 руб.")]
    invoice = pdf_invoice.read_invoice(str(_invoice(tmp_path, _ROWS, below)))

    assert len(invoice.warnings) == 2
    assert "всего наименований 3" in invoice.warnings[0]
    assert "34 044,00" in invoice.warnings[1] and "40 000,00" in invoice.warnings[1]


# --- отказы -----------------------------------------------------------------------

def test_scan_without_text_is_explained(tmp_path: Path) -> None:
    lines, _ = _table([_HEADER] + _ROWS)
    path = _pdf(tmp_path / "скан.pdf", [_page(lines, [])])
    with pytest.raises(pdf_invoice.PdfProblem, match="скан"):
        pdf_invoice.read_invoice(str(path))


def test_text_without_table_is_explained(tmp_path: Path) -> None:
    path = _pdf(tmp_path / "письмо.pdf", [_page([], [(30, 700, "Добрый день")])])
    with pytest.raises(pdf_invoice.PdfProblem, match="таблица"):
        pdf_invoice.read_invoice(str(path))


def test_broken_file_is_explained(tmp_path: Path) -> None:
    path = tmp_path / "битый.pdf"
    path.write_bytes(b"not a pdf at all")
    with pytest.raises(pdf_invoice.PdfProblem):
        pdf_invoice.read_invoice(str(path))


# --- книга Excel ------------------------------------------------------------------

def test_converted_workbook_opens_like_any_price(tmp_path: Path) -> None:
    """Колонки книги программа узнаёт сама — её сразу берут в «Заказ»."""
    target, _ = pdf_invoice.convert(str(_invoice(tmp_path, _ROWS, _BELOW_OK)))

    assert target == str(tmp_path / "счёт.xlsx")
    sheet = workbook.load_sheet(target)
    roles = {column.title: column.role for column in sheet.columns}
    assert roles["Артикул"] == FieldRole.ARTICLE
    assert roles["Товары"] == FieldRole.NAME
    assert roles["Кол-во"] == FieldRole.QUANTITY
    assert roles["Цена"] == FieldRole.PRICE
    assert len(sheet.records) == 2
    assert workbook.list_sheets(target) == ["Счёт", "Документ"]


def test_article_is_text_in_workbook(tmp_path: Path) -> None:
    target, _ = pdf_invoice.convert(str(_invoice(tmp_path, _ROWS)))
    rows, _ = workbook.read_raw(target)
    assert rows[1][1] == "0022"
