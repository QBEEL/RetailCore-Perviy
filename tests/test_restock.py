"""Запас и заказ: разбор отчёта 1С по брендам, расчёт, Excel и вкладка.

Строки собираются здесь же, в том виде, в каком их печатает 1С: двухэтажная шапка
с названием склада над блоком из шести колонок, итог бренда, итог категории и
товары. Период в таком файле не записан — он задаётся при расчёте.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import load_workbook

from app.core import restock
from app.core.order import detect_source, read_orders
from app.core.restock import Params, parse_rows
from app.core.settings import AppSettings

HEAD = (["Производитель", "Категория", "Артикул", "Номенклатура", "Характеристика",
         "Штрихкод", "Статус", "НовыйСтатус", "ЗафиксироватьСтатус", "ТекущийОстаток",
         "Продажи", "СуммаПродаж",
         "Основной склад", None, None, None, "Рекомендовано", "ИтогоЗаказ",
         "Магазин А", None, None, None, None, None,
         "Магазин Б", None, None, None, None, None, "Заказ поставщику"])
SUB = ([None] * 12
       + ["Остаток на складе", "Резерв", "В пути", "Свободный остаток", None, None]
       + ["Остаток на складе", "Продажи", "Резерв", "В пути", "Свободный остаток", "Заказ"] * 2
       + [None])

# Период для расчётов: 61 день ≈ 2 месяца, так что «продажи в месяц» ≈ половина продаж.
PARAMS = Params(date(2026, 1, 1), date(2026, 3, 2))


def _total(brand: str, category: str, stock: float, sales: float) -> list:
    row = [None] * len(HEAD)
    row[0], row[1] = brand, category or None
    row[8], row[9], row[10] = "Нет", stock, sales
    return row


def _item(brand: str, category: str, article: str | None, name: str, *,
          main: tuple[float, float] = (0, 0), a: tuple[float, float, float] = (0, 0, 0),
          b: tuple[float, float, float] = (0, 0, 0), status: str | None = "A",
          barcode: str | None = None, recommended: float | None = None) -> list:
    """main — (остаток, в пути); a и b — (остаток, продажи, в пути)."""
    row = [None] * len(HEAD)
    stock = main[0] + a[0] + b[0]
    sales = a[1] + b[1]
    row[0:7] = [brand, category, article, name, "50 мл", barcode, status]
    row[8], row[9], row[10], row[11] = "Нет", stock or None, sales or None, sales * 1000.0 or None
    row[12], row[14] = main[0] or None, main[1] or None
    row[16] = recommended
    row[18], row[19], row[21] = a[0] or None, a[1] or None, a[2] or None
    row[24], row[25], row[27] = b[0] or None, b[1] or None, b[2] or None
    return row


def _rows(*parts: list, extra_top: int = 0) -> list[list]:
    return [*([[None] * len(HEAD)] * extra_top), HEAD, SUB, *parts]


ITEMS = [
    _item("ALFA", "Шампуни", "A-1", "Шампунь", a=(2, 30, 0), b=(1, 30, 0), recommended=57),
    _item("ALFA", "Шампуни", "A-2", "Бальзам", main=(100, 0), a=(10, 6, 0), b=(5, 6, 0)),
    _item("ALFA", "Шампуни", "A-3", "Маска", a=(5, 0, 0), b=(5, 0, 0)),
    _item("ALFA", "Уход", None, "Крем без артикула", b=(0, 20, 0), barcode=None, status=None),
]


def _alfa() -> list[list]:
    stock = sum((r[9] or 0) for r in ITEMS)
    sales = sum((r[10] or 0) for r in ITEMS)
    return _rows(
        _total("ALFA", "", stock, sales),
        _total("ALFA", "Шампуни", stock, sales - 20),
        *ITEMS[:3],
        _total("ALFA", "Уход", 0, 20),
        ITEMS[3],
    )


def _plan(params: Params = PARAMS) -> restock.Plan:
    return restock.build(parse_rows(_alfa(), source="отчёт.xlsx"), params)


# --- разбор -----------------------------------------------------------------------------

def test_товары_и_склады_из_двухэтажной_шапки():
    stock = parse_rows(_alfa(), source="отчёт.xlsx")
    assert [item.article for item in stock.items] == ["A-1", "A-2", "A-3", ""]
    assert stock.places == ["Основной склад", "Магазин А", "Магазин Б"]
    # У основного склада колонки «Продажи» нет — он отгружает, а не торгует.
    assert stock.selling == ["Магазин А", "Магазин Б"]
    first = stock.items[0]
    assert (first.stock, first.sales, first.recommended) == (3, 60, 57)
    assert [(p.name, p.stock, p.sales) for p in first.places] == [
        ("Магазин А", 2, 30), ("Магазин Б", 1, 30)]
    assert stock.balanced is True and stock.warnings == []


def test_строки_групп_не_товары():
    stock = parse_rows(_alfa())
    assert len(stock.items) == 4
    assert all(item.name for item in stock.items)


def test_товар_без_артикула_и_штрихкода_различается_названием():
    stock = parse_rows(_alfa())
    assert stock.items[3].key == "Крем без артикула|50 мл"


def test_пустая_ячейка_это_ноль():
    item = parse_rows(_alfa()).items[3]
    assert item.stock == 0 and item.transit == 0 and item.sales == 20


def test_шапка_не_с_первой_строки():
    stock = parse_rows(_rows(*_alfa()[2:], extra_top=3))
    assert len(stock.items) == 4


def test_товар_в_пути_суммируется_по_складам():
    rows = _rows(_item("ALFA", "К", "T-1", "Товар", main=(0, 40), a=(1, 5, 3)))
    assert parse_rows(rows).items[0].transit == 43


def test_несошедшийся_итог_бренда_виден():
    parts = _alfa()
    parts[2][9] = 9999
    stock = parse_rows(parts)
    assert stock.balanced is False
    assert any("ALFA" in text and "итог бренда" in text for text in stock.warnings)


def test_итог_продаж_больше_суммы_по_магазинам_не_ломает_отчёт():
    """Магазин, в котором торговали, в колонки отчёта может не попасть."""
    item = _item("ALFA", "Шампуни", "A-1", "Шампунь", a=(2, 30, 0), b=(1, 30, 0))
    item[10] = 70
    stock = parse_rows(_rows(item))
    assert stock.items[0].sales == 70
    assert any("итог продаж" in text for text in stock.warnings)


def test_несколько_брендов():
    parts = _alfa() + [
        _total("BETA", "", 7, 3),
        _total("BETA", "Крем", 7, 3),
        _item("BETA", "Крем", "B-1", "Крем", a=(7, 3, 0)),
    ]
    stock = parse_rows(parts)
    assert stock.brands == ["ALFA", "BETA"]
    assert stock.balanced is True


def test_чужой_файл_объясняет_чего_нет():
    with pytest.raises(restock.ParseError, match="Не нашлась шапка"):
        parse_rows([["Номенклатура", "Расход"], ["Паста", 1]])


def test_без_продаж_нельзя_посчитать_запас():
    rows = [["Артикул", "Номенклатура", "Статус", "ТекущийОстаток"], ["1", "Паста", "A", 5]]
    with pytest.raises(restock.ParseError, match="продажи"):
        parse_rows(rows)


def test_шапка_без_товаров():
    with pytest.raises(restock.ParseError, match="ни одного товара"):
        parse_rows(_rows())


# --- расчёт -----------------------------------------------------------------------------

def test_параметры_проверяются():
    stock = parse_rows(_alfa())
    with pytest.raises(ValueError, match="раньше"):
        restock.build(stock, Params(date(2026, 3, 2), date(2026, 1, 1)))
    with pytest.raises(ValueError, match="больше нуля"):
        restock.build(stock, Params(date(2026, 1, 1), date(2026, 3, 2), target=0))
    with pytest.raises(ValueError, match="меньше порога"):
        restock.build(stock, Params(date(2026, 1, 1), date(2026, 3, 2), low=3, high=2))
    with pytest.raises(ValueError, match="больше порога"):
        restock.build(stock, Params(date(2026, 1, 1), date(2026, 3, 2), target=7, high=6))


def test_месяцы_периода():
    assert PARAMS.days == 61
    assert PARAMS.months == pytest.approx(61 / 30.4375)


def test_продажи_в_месяц_запас_и_заказ():
    row = _plan().rows[0]
    months = PARAMS.months
    assert row.per_month == pytest.approx(60 / months)
    assert row.position == 3
    assert row.cover == pytest.approx(3 / (60 / months))
    # Заказ добирает позицию до двух месяцев продаж, вверх до штуки.
    assert row.order == int(-(-(60 / months * 2 - 3) // 1))
    assert row.state == restock.LOW


def test_состояния():
    plan = _plan()
    states = {row.item.article or "без артикула": row.state for row in plan.rows}
    assert states["A-1"] == restock.LOW
    assert states["A-2"] == restock.HIGH, "сто штук на складе при шести продажах в месяц"
    assert states["A-3"] == restock.IDLE, "остаток есть, продаж нет"
    assert states["без артикула"] == restock.OUT, "продажи были, остатка нет"


def test_нет_движения():
    stock = parse_rows(_rows(_item("ALFA", "К", "Z-1", "Пусто")))
    row = restock.build(stock, PARAMS).rows[0]
    assert row.state == restock.DEAD and row.order == 0


def test_в_пути_считается_запасом_и_второй_раз_не_заказывается():
    base = _item("ALFA", "К", "T-1", "Товар", a=(0, 40, 0))
    ordered = restock.build(parse_rows(_rows(base)), PARAMS).rows[0]
    onway = _item("ALFA", "К", "T-1", "Товар", main=(0, 100), a=(0, 40, 0))
    covered = restock.build(parse_rows(_rows(onway)), PARAMS).rows[0]
    assert ordered.state == restock.OUT and ordered.order > 0
    assert covered.state != restock.OUT and covered.order == 0


def test_отрицательный_остаток_не_раздувает_заказ():
    plan = restock.build(
        parse_rows(_rows(_item("ALFA", "К", "N-1", "Минус", a=(-5, 40, 0)))), PARAMS)
    row = plan.rows[0]
    assert row.position == -5 and row.state == restock.OUT
    assert row.order == int(-(-(row.per_month * 2) // 1))
    assert any("Отрицательный остаток" in text for text in plan.warnings)


def test_заказ_ноль_когда_запаса_хватает():
    assert _plan().rows[1].order == 0


def test_ручная_правка_заменяет_расчёт():
    plan = _plan()
    row = plan.rows[0]
    calculated = row.order
    row.manual = calculated + 5
    assert row.qty == calculated + 5 and row.changed
    row.manual = calculated
    assert not row.changed
    row.manual = None
    assert row.qty == calculated


def test_короткий_период_и_кончившийся_товар_предупреждают():
    plan = _plan(Params(date(2026, 3, 1), date(2026, 3, 10)))
    assert any("Период всего 10 дн." in text for text in plan.warnings)
    assert any("остатка нет, а продажи были" in text for text in plan.warnings)


def test_итоги_по_брендам_и_состояниям():
    plan = _plan()
    (group,) = plan.brands()
    assert group.name == "ALFA" and group.items == 4
    assert group.sales == 92 and group.stock == 128
    assert group.ordered_items == len(plan.to_order()) == 2
    assert group.order == sum(row.qty for row in plan.rows)
    counts = plan.counts()
    assert counts[restock.OUT] == 1 and counts[restock.IDLE] == 1


def test_итоги_по_складам():
    places = {place.name: place for place in _plan().places}
    shop_b = places["Магазин Б"]
    assert shop_b.sales == 56 and shop_b.stock == 11
    # В магазине Б продавали четыре товара, а остаток есть у трёх.
    assert shop_b.sold_items == 3 and shop_b.missing_items == 1
    assert places["Основной склад"].selling is False
    assert places["Основной склад"].stock == 100 and places["Основной склад"].cover is None


# --- Excel ------------------------------------------------------------------------------

def test_первый_лист_открывается_во_вкладке_заказ(tmp_path):
    plan = _plan()
    plan.rows[0].manual = 77
    path = restock.save(plan, str(tmp_path / "запас.xlsx"))
    book = load_workbook(path)
    assert book.sheetnames == ["К заказу", "Главное", "Все товары", "По складам"]

    source = detect_source(path)
    assert source.sheet == "К заказу"
    assert source.titles[source.quantity] == "ИтогоЗаказ"
    quantities = {line.article: line.quantity for line in read_orders(source)}
    assert quantities["A-1"] == 77, "в файл уходит то, что на экране, а не расчёт"
    assert "A-2" not in quantities and "A-3" not in quantities, "нулевой заказ не выгружается"


def test_все_товары_показывают_расчёт_и_правку(tmp_path):
    plan = _plan()
    plan.rows[0].manual = 77
    sheet = load_workbook(restock.save(plan, str(tmp_path / "запас.xlsx")))["Все товары"]
    titles = [cell.value for cell in sheet[1]]
    first = dict(zip(titles, [cell.value for cell in sheet[2]]))
    assert first["Расчёт заказа"] == plan.rows[0].order and first["ИтогоЗаказ"] == 77
    assert first["Рекомендует 1С"] == 57


def test_выгрузка_одного_бренда(tmp_path):
    parts = _alfa() + [
        _total("BETA", "", 9, 100),
        _total("BETA", "Крем", 9, 100),
        _item("BETA", "Крем", "B-1", "Крем", a=(9, 100, 0)),
    ]
    plan = restock.build(parse_rows(parts), PARAMS)
    path = restock.save(plan, str(tmp_path / "бета.xlsx"), brand="BETA")
    sheet = load_workbook(path)["К заказу"]
    assert [row[2] for row in sheet.iter_rows(min_row=2, values_only=True)] == ["B-1"]
    assert "BETA" in restock.default_name(str(tmp_path), plan, "BETA")


def test_главное_называет_допущения(tmp_path):
    sheet = load_workbook(restock.save(_plan(), str(tmp_path / "з.xlsx")))["Главное"]
    text = " ".join(str(cell.value) for row in sheet.iter_rows() for cell in row if cell.value)
    assert "01.01.2026 — 02.03.2026" in text
    assert "не записан" in text and "Кратность" in text


# --- настройки --------------------------------------------------------------------------

def test_сроки_запоминаются_а_период_нет(tmp_path):
    path = str(tmp_path / "settings.json")
    settings = AppSettings.load(path)
    settings.restock_target, settings.restock_low, settings.restock_high = 3.0, 0.5, 8.0
    settings.save()
    loaded = AppSettings.load(path)
    assert (loaded.restock_target, loaded.restock_low, loaded.restock_high) == (3.0, 0.5, 8.0)
    assert not hasattr(loaded, "restock_start")


def test_кривые_сроки_в_настройках_игнорируются(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"restock_target": "много", "restock_low": -1}', encoding="utf-8")
    loaded = AppSettings.load(str(path))
    assert (loaded.restock_target, loaded.restock_low) == (2.0, 1.0)


# --- вкладка ----------------------------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.ui.widgets.restock_tab import ORDER_COLUMN, RestockTab
from app.ui.widgets.toast import ToastKind


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def tab(application, tmp_path):
    messages: list[tuple[str, ToastKind]] = []
    widget = RestockTab(AppSettings.load(str(tmp_path / "settings.json")),
                        lambda text, kind: messages.append((text, kind)))
    widget.messages = messages  # type: ignore[attr-defined]
    widget._loaded(parse_rows(_alfa(), source="отчёт.xlsx"))
    return widget


def _fill_period(tab: RestockTab) -> None:
    tab.start_input.setText("01.01.2026")
    tab.end_input.setText("02.03.2026")


def test_поля_периода_пустые_и_без_периода_не_считается(tab):
    assert tab.start_input.text() == "" and tab.end_input.text() == ""
    tab.calculate()
    assert tab._plan is None
    assert tab.messages[-1][1] is ToastKind.WARNING
    assert "период" in tab.messages[-1][0].lower()


def test_подпись_периода(tab):
    _fill_period(tab)
    assert "61 дн." in tab.span_label.text()
    tab.end_input.setText("01.12.2025")
    assert "раньше" in tab.span_label.text()


def test_расчёт_показывает_к_заказу_срочное_первым(tab):
    _fill_period(tab)
    tab.calculate()
    table = tab.items_table
    assert table.rowCount() == 2, "по умолчанию — только то, что нужно заказать"
    assert [row.item.article for row in tab._shown] == ["", "A-1"], "нет в наличии раньше, чем мало"
    assert tab.tile_order._value.text() == "2"
    assert tab.save_button.isEnabled()


def test_правка_количества_обновляет_итоги(tab):
    _fill_period(tab)
    tab.calculate()
    row = tab._shown[1]
    calculated = row.order
    cell = tab.items_table.item(1, ORDER_COLUMN)
    cell.setText("5")
    assert row.qty == 5 and row.manual == (None if calculated == 5 else 5)
    assert tab.tile_qty._value.text().replace(" ", "") == str(tab._shown[0].qty + 5)


def test_неверное_количество_возвращается(tab):
    _fill_period(tab)
    tab.calculate()
    row = tab._shown[1]
    before = row.qty
    tab.items_table.item(1, ORDER_COLUMN).setText("много")
    assert row.qty == before
    assert tab.items_table.item(1, ORDER_COLUMN).text() == str(before)
    assert tab.messages[-1][1] is ToastKind.WARNING


def test_фильтр_по_состоянию_и_поиск(tab):
    _fill_period(tab)
    tab.calculate()
    tab.state_box.setCurrentText(restock.IDLE)
    assert [row.item.article for row in tab._shown] == ["A-3"]
    tab.state_box.setCurrentText("Любое состояние")
    tab.search_input.setText("крем")
    assert [row.item.name for row in tab._shown] == ["Крем без артикула"]


def test_пересчёт_сбрасывает_правки(tab):
    _fill_period(tab)
    tab.calculate()
    tab.items_table.item(0, ORDER_COLUMN).setText("999")
    assert tab._shown[0].manual == 999
    tab.calculate()
    assert all(row.manual is None for row in tab._plan.rows)


def test_сроки_сохраняются_в_настройках_при_расчёте(tab):
    _fill_period(tab)
    tab.target_input.setValue(3.0)
    tab.calculate()
    assert tab.settings.restock_target == 3.0


def test_неверные_сроки_объясняются(tab):
    _fill_period(tab)
    tab.target_input.setValue(9.0)
    tab.high_input.setValue(6.0)
    tab.calculate()
    assert tab._plan is None
    assert "порога" in tab.messages[-1][0]


def test_вкладка_есть_в_отчётности_и_закрывается_отдельно(application):
    from app.ui.pages import parts_of
    from app.ui.reports_page import ReportsPage

    page = ReportsPage(AppSettings(), lambda *_: None)
    titles = [page.tabs.tabText(i) for i in range(page.tabs.count())]
    assert titles[-1] == "Запас и заказ"
    assert parts_of("reports")[-1].code == "reports.restock"
    page.tabs.setCurrentIndex(len(titles) - 1)
    assert "период выгрузки" in page.subtitle.text()
