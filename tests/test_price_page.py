"""«Быстрая смена цен»: в таблице видны все виды цен, а не только первый.

Раньше в таблице стояла одна пара «старая/новая» — первого вида цены, обычно
себестоимости. Как поменялась РРЦ, было видно, только если открыть строку.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.models import Record  # noqa: E402
from app.core.pricing import ComparisonResult, PriceCell, PriceLine, PriceStatus, PriceType  # noqa: E402
from app.core.settings import AppSettings  # noqa: E402
from app.ui import price_page  # noqa: E402
from app.ui.price_page import PricePage  # noqa: E402

TYPES = [PriceType("Себестоимость", price_column=5), PriceType("РРЦ", price_column=7)]


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(application, monkeypatch):
    # Ключи привязок строятся по загруженному шаблону, а колонкам он не нужен.
    monkeypatch.setattr(price_page.suppliers, "keys_for", lambda template, lines: [])
    return PricePage(AppSettings(), lambda *args: None)


def _line(cost: tuple, retail: tuple) -> PriceLine:
    line = PriceLine(record=Record(row=2, values=[]), row=2, article="A-1", name="Крем",
                     status=PriceStatus.CHANGED)
    line.cells = [PriceCell(0, *cost), PriceCell(1, *retail)]
    return line


def _table(page: PricePage) -> dict[str, str]:
    """Значения первой строки по полному имени колонки («Δ % РРЦ», а не «Δ %»)."""
    model = page.table.model_
    return {(column.tip or column.title): model.index(0, c).data()
            for c, column in enumerate(model.columns)}


def _compare(page: PricePage, lines: list[PriceLine]) -> None:
    page._on_compared(ComparisonResult(lines=lines, types=TYPES))


def test_every_price_type_has_its_columns(page) -> None:
    _compare(page, [_line((1000.0, 1100.0), (2000.0, 2400.0))])

    shown = _table(page)
    assert shown["Себестоимость"] == "1 000 → 1 100"
    assert shown["Δ % Себестоимость"] == "+10.0 %"
    assert shown["РРЦ"] == "2 000 → 2 400"
    assert shown["Δ % РРЦ"] == "+20.0 %"


def test_unchanged_and_missing_prices(page) -> None:
    """Без изменений — одна цена; поставщик цену не дал — остаётся прежняя."""
    _compare(page, [_line((1000.0, 1000.0), (2000.0, None))])

    shown = _table(page)
    assert shown["Себестоимость"] == "1 000"
    assert shown["Δ % Себестоимость"] == ""
    assert shown["РРЦ"] == "2 000"
    assert shown["Δ % РРЦ"] == ""


def test_columns_kept_when_types_repeat(page) -> None:
    """Повторное сравнение с теми же видами не сбрасывает подогнанные ширины."""
    _compare(page, [_line((1.0, 2.0), (3.0, 4.0))])
    retail = [column.title for column in page.table.model_.columns].index("РРЦ")
    page.table.horizontalHeader().resizeSection(retail, 333)

    _compare(page, [_line((1.0, 2.0), (3.0, 5.0))])

    assert page.table.horizontalHeader().sectionSize(retail) == 333


def test_prices_follow_the_product_name(page) -> None:
    """До РРЦ не нужно листать вправо: цены стоят сразу за товаром."""
    _compare(page, [_line((1.0, 2.0), (3.0, 4.0))])
    titles = [column.title for column in page.table.model_.columns]
    assert titles[titles.index("Товар") + 1:titles.index("Товар") + 5] == [
        "Себестоимость", "Δ %", "РРЦ", "Δ %"]
    tips = [page.table.model_.headerData(c, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole)
            for c in range(len(titles))]
    assert "Δ % РРЦ" in tips
