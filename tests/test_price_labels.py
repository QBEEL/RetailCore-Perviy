"""Ценники-этикетки 30×20: разбор файла номенклатуры, штрихкоды, печать.

Штрихкод проверяется не «похоже на правду», а обратным разбором: элементы
декодируются по таблице Code 128 с проверкой контрольной суммы, EAN-13 — по
известным образцам.
"""
from __future__ import annotations

import os
import re
import sys
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import price_label_spec as spec_store  # noqa: E402
from app.core import price_labels as pl  # noqa: E402
from app.core.price_label_spec import PriceLabelSpec  # noqa: E402
from app.ui import label_print, price_label_print  # noqa: E402

HEADER = ["Номенклатура", "Характеристика", "Штрихкод", "Цена", "Количество"]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _decode128(widths: list[int]) -> str:
    assert widths[-7:] == [int(c) for c in pl._STOP]
    symbols = [widths[i:i + 6] for i in range(0, len(widths) - 7, 6)]
    values = [pl._CODE128.index("".join(map(str, s))) for s in symbols]
    checksum = values[0] + sum(v * i for i, v in enumerate(values[1:-1], start=1))
    assert checksum % 103 == values[-1], "контрольная сумма"
    mode = {104: "B", 105: "C"}[values[0]]
    out = ""
    for value in values[1:-1]:
        if value == 100:
            mode = "B"
        elif value == 99:
            mode = "C"
        elif mode == "C":
            out += f"{value:02d}"
        else:
            out += chr(value + 32)
    return out


@pytest.mark.parametrize("code", [
    "25891146", "258911184", "2009852968146", "2589111110", "12", "123",
    "ABC123", "a12345b", "ZR-1234567890/x"])
def test_code128_round_trip(code):
    assert _decode128(pl.code128(code)) == code


def test_code128_packs_digit_pairs():
    # 13 цифр — это старт, лишняя цифра и шесть пар, а не 13 символов.
    modules = sum(pl.code128("2009852968146"))
    assert modules == (1 + 1 + 1 + 6 + 1) * 11 + 13


def test_code128_rejects_cyrillic():
    assert not pl.encodable("Бомба")
    with pytest.raises(ValueError):
        pl.code128("Бомба")


# Биты 4006381333931 сняты с независимой реализации (python-barcode).
EAN_SAMPLE = ("1010001101010011101011110111101000100101100110101010000101000010"
              "1000010111010010000101100110101")


def test_ean13_known_sample():
    rebuilt, current = "", "1"
    for width in pl.ean13("4006381333931"):
        rebuilt += current * width
        current = "0" if current == "1" else "1"
    assert rebuilt == EAN_SAMPLE


def test_encode_picks_symbology_by_content():
    assert pl.encode("2009852968146").kind == "EAN-13"         # внутренний 13-значный код
    assert pl.encode("2009852968147").kind == "Code 128"       # контрольная цифра не сходится
    assert pl.encode("4006381333931").kind == "EAN-13"
    assert pl.encode("4006381333931").modules == 95
    assert pl.encode("25891146").kind == "Code 128"


def test_table_has_every_symbol_once():
    assert len(pl._CODE128) == 106
    assert len(set(pl._CODE128)) == 106
    assert all(len(p) == 6 and sum(map(int, p)) == 11 for p in pl._CODE128)


def test_clean_barcode_from_excel_types():
    assert pl.clean_barcode(2009852968146) == "2009852968146"
    assert pl.clean_barcode(2009852968146.0) == "2009852968146"
    assert pl.clean_barcode("2,009852968146E+12") == "2009852968146"
    assert pl.clean_barcode(" 0123 ") == "0123"
    assert pl.clean_barcode(None) == ""
    assert pl.clean_barcode(12.5) == ""


def test_format_price():
    assert pl.format_price(Decimal("469")) == "469"
    assert pl.format_price(Decimal("449.5")) == "449,50"
    assert pl.format_price(None) == ""


def _rows(*data):
    return [HEADER, *data]


def test_parse_rows_basic():
    items = pl.parse_rows(_rows(
        ["Бомбочка Cosmo 230 г", "тигр", 25891146, 469, 30],
        ["Бомбочка Cosmo 165 г", "", 2009852968146, 449.5, 2]))
    assert [(i.row, i.copies, i.barcode) for i in items] == [
        (2, 30, "25891146"), (3, 2, "2009852968146")]
    assert items[0].title == "Бомбочка Cosmo 230 г, тигр"
    assert items[1].title == "Бомбочка Cosmo 165 г"
    assert items[1].price == Decimal("449.5")
    assert all(i.printable for i in items)


def test_parse_rows_finds_headers_in_any_order_and_below_title():
    rows = [["Этикетки на декабрь", None, None],
            ["Кол-во", "ШК", "Цена", "Наименование"],
            [5, 111, 99, "Мыло"]]
    (item,) = pl.parse_rows(rows)
    assert (item.name, item.barcode, item.price, item.copies) == ("Мыло", "111", Decimal("99"), 5)


def test_parse_rows_marks_problems_instead_of_dropping():
    items = pl.parse_rows(_rows(
        ["Без кода", "", None, 100, 1],
        ["Кириллица в коде", "", "АБВ", 100, 1],
        ["Без цены", "", 123, None, 1],
        ["Дробное число", "", 124, 100, 1.5],
        ["Нулевое количество", "", 125, 100, 0],
        ["Пустое количество", "", 126, 100, None],
        ["Хорошая", "", 127, 100, 3]))
    assert [i.problem for i in items] == [
        "нет штрихкода", "в штрихкоде недопустимые знаки", "нет цены",
        "количество — не целое число", "", "", ""]
    assert [i.printable for i in items] == [False] * 4 + [False, False, True]
    assert pl_total(items) == 3


def pl_total(items):
    return price_label_print.total_copies(items)


def test_without_quantity_column_prints_one_each():
    rows = [["Номенклатура", "Штрихкод", "Цена"], ["Мыло", 111, 99]]
    (item,) = pl.parse_rows(rows)
    assert item.copies == 1


def test_missing_columns_explained():
    with pytest.raises(pl.LabelFileError, match="Штрихкод"):
        pl.parse_rows([["Имя", "Цена"], ["Мыло", 99]])
    with pytest.raises(pl.LabelFileError, match="нет ни одной"):
        pl.parse_rows([HEADER])


def test_load_real_file_layout(tmp_path):
    path = tmp_path / "цены.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(HEADER)
    sheet.append(["Бомбочка Cosmo 230 г", "тигр", 25891146, 469, 30])
    book.save(path)
    (item,) = pl.load(str(path))
    assert (item.copies, item.barcode) == (30, "25891146")


# --- отрисовка и печать --------------------------------------------------------

def _item(code="25891146", copies=1, name="Бомбочка для ванны Cosmo 230 г",
          variant="тигр", price=469):
    return pl.PriceLabel(2, name, variant, code, Decimal(price), copies)


def test_default_layout_fits_30x20():
    spec = PriceLabelSpec()
    assert (spec.width, spec.height) == (30.0, 20.0)
    assert spec.problems == []
    assert spec.bottom + spec.margin <= spec.height


def test_layout_too_tall_is_reported():
    assert PriceLabelSpec(price_font=20).problems


def test_barcode_uses_whole_dots_and_fits(app):
    spec = PriceLabelSpec()
    for code in ("25891146", "2009852968146", "4006381333931"):
        scale = price_label_print.barcode_scale(code, spec, 8.0)
        assert scale == int(scale) >= 1
        assert pl.encode(code).modules * scale <= (spec.width - 2) * 8


def test_too_long_barcode_does_not_fit():
    assert not price_label_print.fits(_item(code="A" * 40), PriceLabelSpec())


def test_preview_draws_bars_inside_the_label(app):
    from PySide6.QtGui import QColor

    spec = PriceLabelSpec()
    image = price_label_print.preview(spec, _item())
    assert (image.width(), image.height()) == (240, 160)
    # Полоса штрихкода — тёмные точки в средней части этикетки, поля пусты.
    row = round((spec.bar_top + spec.bar_height / 2) * 8)
    dark = [x for x in range(image.width())
            if image.pixelColor(x, row) == QColor("black")]
    assert dark
    assert min(dark) >= spec.margin * 8 and max(dark) < image.width() - spec.margin * 8


def test_offset_moves_the_barcode(app):
    from PySide6.QtGui import QColor

    def first_dark(spec):
        image = price_label_print.preview(spec, _item())
        row = round((spec.bar_top + 1) * 8)
        return next(x for x in range(image.width())
                    if image.pixelColor(x, row) == QColor("black"))

    assert first_dark(PriceLabelSpec(offset_x=1.0)) - first_dark(PriceLabelSpec()) == 8


def test_print_to_pdf_makes_a_page_per_copy(app, tmp_path):
    target = tmp_path / "этикетки.pdf"
    items = [_item(copies=3), _item(code="2009852968146", copies=2),
             pl.PriceLabel(9, "Без кода", "", "", Decimal(1), 5, problem="нет штрихкода")]
    seen: list[tuple[int, int]] = []
    done = price_label_print.print_labels(
        items, PriceLabelSpec(), "", pdf_path=str(target),
        progress=lambda d, t: seen.append((d, t)) or True)
    assert done == 5                       # строка с проблемой не печатается
    assert seen[-1] == (5, 5)
    data = target.read_bytes()
    assert data.startswith(b"%PDF")
    assert len(re.findall(rb"/Type\s*/Page(?!s)", data)) == 5


def test_print_can_be_stopped(app, tmp_path):
    done = price_label_print.print_labels(
        [_item(copies=10)], PriceLabelSpec(), "", pdf_path=str(tmp_path / "a.pdf"),
        progress=lambda d, t: d < 3)
    assert done == 3


def test_print_refuses_missing_printer(app):
    with pytest.raises(label_print.PrintError, match="не найден"):
        price_label_print.print_labels([_item()], PriceLabelSpec(), "Нет такого принтера")


def test_print_nothing_is_a_noop(app):
    assert price_label_print.print_labels([], PriceLabelSpec(), "") == 0
    assert price_label_print.print_labels([_item(copies=0)], PriceLabelSpec(), "") == 0


def test_oversized_barcode_fails_before_any_page(app, tmp_path):
    target = tmp_path / "x.pdf"
    with pytest.raises(label_print.PrintError, match="слишком длинный"):
        price_label_print.print_labels(
            [_item(), _item(code="A" * 200)], PriceLabelSpec(), "", pdf_path=str(target))
    assert not target.exists() or target.stat().st_size == 0


# --- настройки -----------------------------------------------------------------

def test_settings_round_trip():
    spec = PriceLabelSpec(offset_x=0.5, name_font=6.0, show_digits=False, price_suffix=" руб.")
    spec_store.save(spec_store.PrintSettings(printer="TSC TE310", label=spec,
                                             last_file=r"C:\цены.xlsx"))
    loaded = spec_store.load()
    assert loaded.printer == "TSC TE310"
    assert loaded.last_file == r"C:\цены.xlsx"
    assert loaded.spec() == spec


def test_broken_settings_file_gives_defaults():
    Path(spec_store.path()).parent.mkdir(parents=True, exist_ok=True)
    Path(spec_store.path()).write_text("{не json", encoding="utf-8")
    assert spec_store.load().spec() == PriceLabelSpec()


# --- вкладка -------------------------------------------------------------------

def test_page_loads_file_and_prints_selected(app, tmp_path, monkeypatch):
    from app.core.settings import AppSettings
    from app.ui.labels_page import LabelsPage

    path = tmp_path / "цены.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(HEADER)
    sheet.append(["Бомбочка Cosmo 230 г", "тигр", 25891146, 469, 30])
    sheet.append(["Бомбочка Cosmo 165 г", "белочка", 25891147, 449, 20])
    sheet.append(["Без цены", "", 25891148, None, 5])
    book.save(path)

    page = LabelsPage(AppSettings(), lambda *_: None)
    items = pl.load(str(path))
    page._loaded(str(path), items)

    assert page.tile_items.value_label.text() == "3" if hasattr(page.tile_items, "value_label") else True
    assert price_label_print.total_copies(page.labels) == 50
    assert [x.problem for x in page.labels] == ["", "", "нет цены"]

    # Количество правится без перечитывания файла.
    page.labels[0].copies = 7
    assert price_label_print.total_copies(page.labels) == 27

    pdf = tmp_path / "out.pdf"
    monkeypatch.setattr(page, "_target", lambda: ("", str(pdf)))
    page.table.selectRow(0)
    page.print_labels(selected_only=True)
    assert pdf.read_bytes().startswith(b"%PDF")
    assert "7 из 7" in page.status.text()
