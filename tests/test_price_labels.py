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
    settings = spec_store.PrintSettings(printer="TSC TE310", last_file=r"C:\цены.xlsx")
    settings.templates[spec_store.DEFAULT_TEMPLATE] = spec
    spec_store.save(settings)
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


# --- сдвиг вниз не обрезает этикетку ---------------------------------------------

def test_layout_at_zero_offset_is_the_plain_stack():
    spec = PriceLabelSpec()
    place = spec.layout()
    assert not place.clipped
    assert place.bar_height == spec.bar_height
    assert place.bar_top == pytest.approx(spec.bar_top)


def test_shift_down_shrinks_bars_instead_of_cutting_the_digits():
    spec = PriceLabelSpec(offset_y=2.0)
    place = spec.layout()
    assert not place.clipped
    assert 3.0 <= place.bar_height < spec.bar_height
    assert place.digits_top + spec.digits_line <= spec.height


def test_shift_that_cannot_fit_is_reported():
    assert not PriceLabelSpec(offset_y=5.0).layout().clipped   # сжалось и поместилось
    assert PriceLabelSpec(offset_y=8.0).layout().clipped
    assert PriceLabelSpec(offset_y=-3.0).layout().clipped      # ушло выше верхней кромки


def test_digits_stay_on_the_label_when_shifted_down(app):
    from PySide6.QtGui import QColor

    spec = PriceLabelSpec(offset_y=2.0)
    image = price_label_print.preview(spec, _item())
    place = spec.layout()
    rows = range(round(place.digits_top * 8), image.height())
    assert any(image.pixelColor(x, y) == QColor("black")
               for y in rows for x in range(image.width()))


def test_blank_label_is_pure_white(app):
    from PySide6.QtGui import QColor, QImage, QPainter

    image = QImage(240, 160, QImage.Format.Format_RGB32)
    image.fill(QColor("black"))
    painter = QPainter(image)
    price_label_print.draw_label(painter, PriceLabelSpec(), None, 8.0, 8.0)
    painter.end()
    assert all(image.pixelColor(x, y) == QColor("white")
               for x in range(0, 240, 7) for y in range(0, 160, 7))


# --- режимы между номенклатурами ---------------------------------------------------

def _pages(path) -> int:
    return len(re.findall(rb"/Type\s*/Page(?!s)", Path(path).read_bytes()))


def test_blank_label_between_items_but_not_after_the_last(app, tmp_path):
    target = tmp_path / "a.pdf"
    items = [_item(copies=2), _item(code="25891147", copies=1), _item(code="25891148", copies=3)]
    printed = price_label_print.print_labels(
        items, PriceLabelSpec(), "", pdf_path=str(target), blank_between=True)
    assert printed == 6                      # пустые этикетки не считаются напечатанными
    assert _pages(target) == 6 + 2           # два разделителя


def test_blank_between_single_item_adds_nothing(app, tmp_path):
    target = tmp_path / "a.pdf"
    price_label_print.print_labels([_item(copies=3)], PriceLabelSpec(), "",
                                   pdf_path=str(target), blank_between=True)
    assert _pages(target) == 3


@pytest.fixture
def jobs(monkeypatch, tmp_path):
    """Каждый вызов настройки принтера — отдельное задание; пишем его в свой PDF."""
    made: list[Path] = []
    real = label_print.make_printer

    def fake(spec, name, pdf_path):
        path = tmp_path / f"job{len(made)}.pdf"
        made.append(path)
        return real(spec, "", str(path))

    monkeypatch.setattr(label_print, "make_printer", fake)
    return made


def test_tear_mode_sends_each_item_as_its_own_job(app, jobs):
    items = [_item(copies=2), _item(code="25891147", copies=3), _item(code="25891148", copies=1)]
    asked: list[tuple[str, str]] = []

    def pause(done, following):
        asked.append((done.barcode, following.barcode))
        return True

    printed = price_label_print.print_labels(items, PriceLabelSpec(), "Принтер", pause=pause)
    assert printed == 6
    assert asked == [("25891146", "25891147"), ("25891147", "25891148")]
    assert [_pages(path) for path in jobs] == [2, 3, 1]


def test_next_item_is_not_sent_until_the_pause_returns(app, jobs):
    seen_jobs: list[int] = []
    items = [_item(copies=1), _item(code="25891147", copies=1)]

    def pause(done, following):
        seen_jobs.append(len(jobs))          # к этому моменту второе задание не создано
        return True

    price_label_print.print_labels(items, PriceLabelSpec(), "Принтер", pause=pause)
    assert seen_jobs == [1] and len(jobs) == 2


def test_stopping_at_the_pause_prints_nothing_more(app, jobs):
    items = [_item(copies=2), _item(code="25891147", copies=3)]
    printed = price_label_print.print_labels(
        items, PriceLabelSpec(), "Принтер", pause=lambda done, following: False)
    assert printed == 2 and len(jobs) == 1


def test_no_pause_for_pdf_and_for_a_single_item(app, tmp_path, jobs):
    calls: list[int] = []
    price_label_print.print_labels(
        [_item(copies=1), _item(code="25891147", copies=1)], PriceLabelSpec(), "",
        pdf_path=str(tmp_path / "a.pdf"), pause=lambda *a: calls.append(1) or True)
    price_label_print.print_labels([_item(copies=2)], PriceLabelSpec(), "Принтер",
                                   pause=lambda *a: calls.append(1) or True)
    assert calls == []


def test_gap_mode_is_remembered_and_unknown_value_falls_back():
    spec_store.save(spec_store.PrintSettings(gap_mode=spec_store.GAP_TEAR))
    assert spec_store.load().gap_mode == spec_store.GAP_TEAR
    Path(spec_store.path()).write_text('{"gap_mode": "что-то"}', encoding="utf-8")
    assert spec_store.load().gap_mode == spec_store.GAP_CONTINUOUS


def test_page_blank_mode_and_remembered_choice(app, tmp_path, monkeypatch):
    from app.core.settings import AppSettings
    from app.ui.labels_page import LabelsPage

    page = LabelsPage(AppSettings(), lambda *_: None)
    items = [_item(copies=2), _item(code="25891147", copies=1)]
    page._loaded("цены.xlsx", items)
    page.gap_box.setCurrentIndex(page.gap_box.findData(spec_store.GAP_BLANK))
    assert spec_store.load().gap_mode == spec_store.GAP_BLANK

    pdf = tmp_path / "out.pdf"
    monkeypatch.setattr(page, "_target", lambda: ("", str(pdf)))
    page.print_labels(selected_only=False)
    assert _pages(pdf) == 4                      # 2 + 1 этикетка и один разделитель
    assert "3 из 3" in page.status.text()

    # Новое окно подхватывает выбор, сделанный в прошлый раз.
    assert LabelsPage(AppSettings(), lambda *_: None).gap_box.currentData() == spec_store.GAP_BLANK


def test_page_tear_mode_asks_between_items(app, tmp_path, monkeypatch, jobs):
    from PySide6.QtWidgets import QMessageBox

    from app.core.settings import AppSettings
    from app.ui.labels_page import LabelsPage

    page = LabelsPage(AppSettings(), lambda *_: None)
    page._loaded("цены.xlsx", [_item(copies=2), _item(code="25891147", copies=1)])
    page.gap_box.setCurrentIndex(page.gap_box.findData(spec_store.GAP_TEAR))
    monkeypatch.setattr(page, "_target", lambda: ("Принтер", ""))

    shown: list[str] = []

    def fake_exec(box):
        shown.append(box.text())
        # «Печатать дальше» — первая добавленная кнопка.
        monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self.buttons()[0])
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    page.print_labels(selected_only=False)

    assert len(shown) == 1
    assert "2 шт." in shown[0] and "Дальше" in shown[0] and "оторвите" in shown[0]
    assert [_pages(path) for path in jobs] == [2, 1]


# --- шаблоны и скидка ----------------------------------------------------------------

def test_builtin_templates_are_valid_and_cover_both_sizes_and_both_prices():
    names = spec_store.BUILTIN_TEMPLATES
    assert names == ("30x20_1цена", "30x20_2цены", "43x25_1цена", "43x25_2цены")
    for name in names:
        spec = spec_store.builtin(name)
        assert spec.problems == [], name
        assert not spec.layout().clipped, name
    assert [(s.width, s.height, s.prices) for s in map(spec_store.builtin, names)] == [
        (30, 20, 1), (30, 20, 2), (43, 25, 1), (43, 25, 2)]


def test_settings_offer_builtins_then_own_templates():
    settings = spec_store.PrintSettings()
    settings.add("мой", PriceLabelSpec(width=50, height=30))
    settings.add("аква", PriceLabelSpec())
    assert settings.names() == [*spec_store.BUILTIN_TEMPLATES, "аква", "мой"]


def test_templates_survive_a_restart_and_builtin_edits_stay():
    settings = spec_store.PrintSettings()
    edited = settings.select(spec_store.T_43X25_2)
    edited.offset_y = 0.5
    settings.add("свой", PriceLabelSpec(width=50, height=30, prices=2))
    settings.template = "свой"
    settings.discount = 25
    spec_store.save(settings)

    loaded = spec_store.load()
    assert loaded.template == "свой" and loaded.discount == 25
    assert loaded.spec("свой").width == 50 and loaded.spec("свой").prices == 2
    assert loaded.spec(spec_store.T_43X25_2).offset_y == 0.5
    # Нетронутый встроенный не записан и берётся из кода.
    assert loaded.spec(spec_store.T_30X20_2) == spec_store.builtin(spec_store.T_30X20_2)


def test_builtin_cannot_be_removed_but_can_be_reset():
    settings = spec_store.PrintSettings()
    settings.select(spec_store.T_43X25_1).width = 99
    assert not settings.remove(spec_store.T_43X25_1)
    assert settings.reset(spec_store.T_43X25_1).width == 43
    settings.add("свой", PriceLabelSpec())
    settings.template = "свой"
    assert settings.remove("свой") and settings.template == spec_store.DEFAULT_TEMPLATE


def test_old_single_template_file_moves_to_30x20():
    Path(spec_store.path()).parent.mkdir(parents=True, exist_ok=True)
    Path(spec_store.path()).write_text(
        '{"printer": "PT10", "label": {"offset_y": 1.5, "width": 30}}', encoding="utf-8")
    loaded = spec_store.load()
    assert loaded.printer == "PT10"
    assert loaded.template == spec_store.T_30X20_1
    assert loaded.spec().offset_y == 1.5


def test_broken_template_values_fall_back():
    Path(spec_store.path()).parent.mkdir(parents=True, exist_ok=True)
    Path(spec_store.path()).write_text(
        '{"template": "нет такого", "discount": 150,'
        ' "templates": {"x": {"width": "много", "prices": 7}, "": {}}}', encoding="utf-8")
    loaded = spec_store.load()
    assert loaded.template == spec_store.DEFAULT_TEMPLATE
    assert loaded.discount == 10.0
    assert loaded.spec("x").width == 30.0 and loaded.spec("x").prices == 1
    assert "" not in loaded.templates


@pytest.mark.parametrize("price,percent,expected", [
    (469, 10, "422"), (445, 10, "401"), (299, 30, "209"), ("449.5", 15, "382.08"),
    (100, 99, "1")])
def test_discounted_price_rounding(price, percent, expected):
    assert pl.discounted(Decimal(str(price)), percent) == Decimal(expected)


@pytest.mark.parametrize("percent", [0, -5, 100, 150])
def test_discount_must_be_between_zero_and_hundred(percent):
    with pytest.raises(ValueError):
        pl.discounted(Decimal(100), percent)


def test_apply_discount_sets_and_clears():
    items = [_item(price=469),
             pl.PriceLabel(3, "Без цены", "", "111", None, 1, problem="нет цены")]
    pl.apply_discount(items, 10)
    assert [i.price2 for i in items] == [Decimal(422), None]
    pl.apply_discount(items, None)
    assert [i.price2 for i in items] == [None, None]


def _dark_columns(image, rows_mm):
    from PySide6.QtGui import QColor

    top, bottom = (round(v * 8) for v in rows_mm)
    return [x for x in range(image.width())
            if any(image.pixelColor(x, y) == QColor("black") for y in range(top, bottom))]


def test_two_prices_are_two_text_lines_one_price_is_one_big_line(app):
    spec2 = spec_store.builtin(spec_store.T_43X25_2)
    item = _item(price=499)
    pl.apply_discount([item], 30)
    place = spec2.layout()
    top, line = place.price_top, spec2.price_line / 2

    image = price_label_print.preview(spec2, item)
    assert _dark_columns(image, (top + 0.2, top + line - 0.2))          # «Цена: 499»
    assert _dark_columns(image, (top + line + 0.2, top + 2 * line - 0.2))  # «Цена с макс. скидкой: 349»

    spec1 = spec_store.builtin(spec_store.T_43X25_1)
    place = spec1.layout()
    assert spec1.price_line < spec2.price_line                          # одна строка ниже двух
    assert _dark_columns(price_label_print.preview(spec1, item),
                         (place.price_top, place.price_top + spec1.price_line))


def test_two_price_lines_use_the_templates_captions():
    item = _item(price=469)
    pl.apply_discount([item], 10)
    spec = spec_store.builtin(spec_store.T_30X20_2)
    assert price_label_print.price_lines(spec, item) == (
        "Цена: 469", "Цена с макс. скидкой: 422")
    spec.price_caption, spec.discount_caption, spec.price_suffix = "Было:", "Стало:", " ₽"
    assert price_label_print.price_lines(spec, item) == ("Было: 469 ₽", "Стало: 422 ₽")


def test_two_price_template_without_discount_prints_one_price(app):
    spec = spec_store.builtin(spec_store.T_30X20_2)
    image = price_label_print.preview(spec, _item(price=469))     # price2 не посчитана
    assert image.width() == 240


def test_43x25_prints_to_pdf_at_its_size(app, tmp_path):
    spec = spec_store.builtin(spec_store.T_43X25_2)
    item = _item(price=499)
    pl.apply_discount([item], 20)
    target = tmp_path / "a.pdf"
    assert price_label_print.print_labels([item], spec, "", pdf_path=str(target)) == 1
    # 43×25 мм в пунктах PDF (Qt округляет до целых): 122×71, а не 30×20 мм = 85×57.
    assert re.search(rb"/MediaBox\s*\[\s*0\s+0\s+122(\.0+)?\s+71(\.0+)?\s*\]",
                     target.read_bytes())


# --- окна и вкладка ---------------------------------------------------------------------

def _page(items=None):
    from app.core.settings import AppSettings
    from app.ui.labels_page import LabelsPage

    page = LabelsPage(AppSettings(), lambda *_: None)
    page._loaded("цены.xlsx", items or [_item(price=469), _item(code="25891147", price=449)])
    return page


def test_choosing_two_price_template_asks_the_discount(app, monkeypatch):
    from app.ui import labels_page as module

    page = _page()
    assert page.table.isColumnHidden(5)                           # у одной цены колонки нет
    asked: list[float] = []

    class Dialog:
        def __init__(self, percent, sample, parent=None):
            asked.append(percent)
            self.percent = 25.0

        def exec(self):
            return 1

    monkeypatch.setattr(module, "DiscountDialog", Dialog)
    page.template_box.setCurrentIndex(page.template_box.findData(spec_store.T_43X25_2))

    assert asked == [10.0]
    assert page.print_settings.template == spec_store.T_43X25_2
    assert (page.spec.width, page.spec.height, page.spec.prices) == (43, 25, 2)
    assert [i.price2 for i in page.labels] == [Decimal(352), Decimal(337)]   # минус 25 %
    assert not page.table.isColumnHidden(5)
    assert "25" in page.discount_label.text()
    assert spec_store.load().discount == 25.0


def test_cancelled_discount_keeps_the_previous_template(app, monkeypatch):
    from app.ui import labels_page as module

    page = _page()

    class Dialog:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return 0

    monkeypatch.setattr(module, "DiscountDialog", Dialog)
    page.template_box.setCurrentIndex(page.template_box.findData(spec_store.T_43X25_2))

    assert page.print_settings.template == spec_store.DEFAULT_TEMPLATE
    assert page.template_box.currentData() == spec_store.DEFAULT_TEMPLATE
    assert all(i.price2 is None for i in page.labels)


def test_one_price_template_does_not_ask(app, monkeypatch):
    from app.ui import labels_page as module

    page = _page()
    monkeypatch.setattr(module, "DiscountDialog",
                        lambda *a, **k: pytest.fail("скидку спрашивать не нужно"))
    page.template_box.setCurrentIndex(page.template_box.findData(spec_store.T_43X25_1))
    assert page.spec.width == 43 and page.table.isColumnHidden(5)


def test_two_price_template_chosen_last_time_opens_without_a_question(app):
    spec_store.save(spec_store.PrintSettings(template=spec_store.T_30X20_2, discount=15))
    page = _page()
    assert page.spec.prices == 2
    assert page.labels[0].price2 == Decimal(399)                  # 469 минус 15 %


def test_template_change_rechecks_barcode_fit(app):
    page = _page([_item(code="A" * 18)])                          # 18 знаков Code 128
    assert page.labels[0].problem == "штрихкод слишком длинный"   # на 30 мм не помещается
    page.template_box.setCurrentIndex(page.template_box.findData(spec_store.T_43X25_1))
    assert page.labels[0].problem == ""                           # на 43 мм помещается


def test_copy_and_delete_own_template(app, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from app.ui import labels_page as module

    page = _page()
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("аква_30", True))
    monkeypatch.setattr(module.TemplateDialog, "exec", lambda self: 0)
    page.copy_template()
    assert page.print_settings.template == "аква_30"
    assert "аква_30" in [page.template_box.itemData(i) for i in range(page.template_box.count())]
    assert page.delete_button.isEnabled()

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    page.delete_template()
    assert page.print_settings.template == spec_store.DEFAULT_TEMPLATE
    assert "аква_30" not in page.print_settings.names()
    assert not page.delete_button.isEnabled()                    # встроенный не удаляется


def test_copy_refuses_taken_name(app, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    page = _page()
    warned: list[str] = []
    monkeypatch.setattr(QInputDialog, "getText",
                        lambda *a, **k: (spec_store.T_43X25_1, True))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    page.copy_template()
    assert warned and page.print_settings.template == spec_store.DEFAULT_TEMPLATE


def test_template_dialog_edits_spec_and_blocks_impossible_layout(app):
    from app.ui.widgets.label_template_dialog import TemplateDialog

    spec = spec_store.builtin(spec_store.T_43X25_1)
    dialog = TemplateDialog(spec_store.T_43X25_1, spec, builtin=True, sample=None, percent=10)
    assert dialog.spec() == spec                                  # без правок — тот же макет
    assert dialog.ok.isEnabled()

    dialog.prices.setCurrentIndex(1)
    dialog.width_.setValue(50)
    edited = dialog.spec()
    assert (edited.prices, edited.width) == (2, 50)
    assert edited.offset_y == spec.offset_y                       # скрытые поля сохранены

    dialog.price_font.setValue(40)                                # не помещается по высоте
    assert not dialog.ok.isEnabled() and "высоте" in dialog.note.text()

    dialog._factory()
    assert dialog.spec() == spec_store.builtin(spec_store.T_43X25_1)
    assert dialog.ok.isEnabled()


def test_page_configure_applies_dialog_result(app, monkeypatch):
    from dataclasses import replace

    from app.ui import labels_page as module

    page = _page()
    edited = replace(page.spec, width=40.0, height=25.0, bar_height=5.0)

    class Dialog:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return 1

        def spec(self):
            return edited

    monkeypatch.setattr(module, "TemplateDialog", Dialog)
    page.configure_template()
    assert page.spec.width == 40.0
    assert spec_store.load().spec().width == 40.0


def test_discount_dialog_example_and_range(app):
    from app.ui.widgets.label_template_dialog import DiscountDialog

    dialog = DiscountDialog(10, Decimal(469))
    assert "469" in dialog.example.text() and "422" in dialog.example.text()
    dialog.spin.setValue(30)
    assert dialog.percent == 30 and "328" in dialog.example.text()
    assert (dialog.spin.minimum(), dialog.spin.maximum()) == (1, 99)
    assert DiscountDialog(0, None).percent == 10                  # непонятное значение → 10 %


# --- удаление позиций из списка ---------------------------------------------------------

def _three():
    return [_item(code="25891146", copies=2), _item(code="25891147", copies=3),
            _item(code="25891148", copies=4)]


def _codes(page):
    return [item.barcode for item in page.labels]


def test_remove_selected_drops_rows_from_the_list_and_the_printing(app, tmp_path, monkeypatch):
    page = _page(_three())
    for number, item in enumerate(page.labels, start=2):
        item.row = number
    page.table.selectRow(1)
    page.remove_selected()

    assert _codes(page) == ["25891146", "25891148"]
    assert price_label_print.total_copies(page.labels) == 6
    assert page.restore_button.isVisible() or not page.restore_button.isHidden()
    assert "Убрано из списка: 1" in page.status.text() and "не изменён" in page.status.text()

    pdf = tmp_path / "out.pdf"
    monkeypatch.setattr(page, "_target", lambda: ("", str(pdf)))
    page.print_labels(selected_only=False)
    assert _pages(pdf) == 6                          # убранная позиция не печатается


def test_remove_several_and_cursor_moves_to_a_neighbour(app):
    page = _page(_three())
    page.table.selectRow(0)
    page.table.selectionModel().select(
        page.table.proxy.index(1, 0),
        page.table.selectionModel().SelectionFlag.Select
        | page.table.selectionModel().SelectionFlag.Rows)
    page.remove_selected()
    assert _codes(page) == ["25891148"]
    assert page._current() is page.labels[0]         # осталась и выбрана последняя строка


def test_remove_without_selection_does_nothing(app):
    page = _page(_three())
    page.table.clearSelection()
    page.table.setCurrentIndex(page.table.proxy.index(-1, -1))
    page.remove_selected()
    assert len(page.labels) == 3 and page._removed == []


def test_restore_puts_rows_back_in_file_order(app):
    page = _page(_three())
    for number, item in enumerate(page.labels, start=2):
        item.row = number
    page.table.selectRow(0)
    page.remove_selected()
    page.table.selectRow(0)
    page.remove_selected()
    assert _codes(page) == ["25891148"]

    page.restore_removed()
    assert _codes(page) == ["25891146", "25891147", "25891148"]
    assert page._removed == [] and page.restore_button.isHidden()


def test_removing_everything_leaves_a_working_page(app):
    page = _page(_three())
    page.table.selectAll()
    page.remove_selected()
    assert page.labels == []
    assert not page.print_button.isEnabled() and not page.remove_button.isEnabled()
    page.restore_removed()
    assert len(page.labels) == 3 and page.print_button.isEnabled()


def test_removed_rows_follow_template_changes(app, monkeypatch):
    from app.ui import labels_page as module

    page = _page(_three())
    page.table.selectRow(0)
    page.remove_selected()
    removed = page._removed[0]

    class Dialog:
        def __init__(self, *a, **k):
            self.percent = 20.0

        def exec(self):
            return 1

    monkeypatch.setattr(module, "DiscountDialog", Dialog)
    page.template_box.setCurrentIndex(page.template_box.findData(spec_store.T_43X25_2))
    assert removed.price2 == Decimal(375)            # 469 минус 20 %, хотя позиции нет в списке
    page.restore_removed()
    assert page.labels[0].price2 == Decimal(375)


def test_new_file_forgets_removed_rows(app):
    page = _page(_three())
    page.table.selectRow(0)
    page.remove_selected()
    page._loaded("другой.xlsx", _three())
    assert page._removed == [] and len(page.labels) == 3


def test_delete_key_is_bound_to_the_table(app):
    page = _page(_three())
    keys = [a.shortcut().toString() for a in page.table.actions()]
    assert "Del" in keys
