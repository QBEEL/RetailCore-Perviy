"""Вкладка «Запас и заказ»: отчёт 1С по брендам → на сколько хватит и сколько заказать.

Сверху — файл и период. Период в отчёте 1С не записан, поэтому его вводит человек,
и поля стоят пустыми: прошлый период на новом файле молча дал бы чужие продажи в
месяц. Пока обе даты не указаны, считать нельзя.

Таблица товаров открывается на тех, что нужно заказать, и самые срочные стоят
первыми: сначала нет в наличии, потом мало. Количество заказа можно править
руками — расчёт знает продажи, но не знает, что поставщик завтра поднимет цену;
поправленное выделено, а в Excel уходит то, что на экране.

Файл читается в фоне: отчёт с тысячей товаров и пятнадцатью складами — это
десятки тысяч ячеек, и замерший интерфейс выглядит как зависшая программа.
"""
from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core import restock
from ...core.restock.analysis import AVG_MONTH
from ...core.settings import AppSettings
from .. import icons
from ..tasks import run_task
from ..theme import Metrics, Palette
from .common import Card, Hint, MetricTile, SectionTitle
from .inputs import DecimalInput, SelectBox
from .payment_dialogs import DateInput
from .toast import ToastKind

RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

ALL_BRANDS = "Все бренды"
TO_ORDER = "К заказу"
ALL_STATES = "Любое состояние"

# Цвет состояния в таблице. «Норма» не красится: нормальное не должно кричать.
STATE_COLORS = {
    restock.OUT: Palette.DANGER,
    restock.LOW: Palette.WARNING,
    restock.HIGH: Palette.INFO,
    restock.IDLE: Palette.TEXT_MUTED,
    restock.DEAD: Palette.TEXT_MUTED,
}

# Что делать с товаром — состояние и количество — стоит сразу за названием: это
# то, ради чего открыта вкладка, и прокручивать таблицу вправо ради него нельзя.
# Цифры расчёта идут следом, справочные колонки 1С — в конце.
ITEM_COLUMNS = ["Бренд", "Артикул", "Товар", "Состояние", "К заказу", "Остаток", "В пути",
                "Продажи", "В месяц", "Хватит, мес.", "Статус 1С", "1С советует"]
ORDER_COLUMN = ITEM_COLUMNS.index("К заказу")
STATE_COLUMN = ITEM_COLUMNS.index("Состояние")
NAME_COLUMN = ITEM_COLUMNS.index("Товар")
TEXT_COLUMNS = {"Бренд", "Артикул", "Товар", "Состояние", "Статус 1С"}


def _qty(value: float | None) -> str:
    """Штуки. Пусто вместо нуля: ноль в свободной клетке только шумит."""
    if not value:
        return ""
    return f"{value:,.0f}".replace(",", " ")


def _decimal(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:,.1f}".replace(",", " ")


class RestockTab(QWidget):
    """Запас и заказ по отчёту 1С."""

    def __init__(self, settings: AppSettings,
                 notify: Callable[[str, ToastKind], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._path = ""
        self._stock: restock.Stock | None = None
        self._plan: restock.Plan | None = None
        self._shown: list[restock.Row] = []
        self._busy = False
        self._filling = False
        self._build()

    # --- разметка --------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, Metrics.GAP, 0, 0)
        root.setSpacing(Metrics.GAP)
        root.addWidget(self._source_card())
        root.addWidget(self._result_card(), 1)

    def _source_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Отчёт по брендам из 1С", card))
        header.addStretch(1)
        self.pick_button = self._action(card, "Выбрать файл", "open", self.pick)
        self.calc_button = self._action(card, "Рассчитать", "run", self.calculate)
        self.calc_button.setObjectName("Primary")
        self.calc_button.setEnabled(False)
        self.save_button = self._action(card, "Сохранить в Excel", "export", self.save)
        self.save_button.setEnabled(False)
        for button in (self.pick_button, self.calc_button, self.save_button):
            header.addWidget(button)
        body.addLayout(header)

        self.path_label = QLabel("Файл не выбран", card)
        self.path_label.setObjectName("Path")
        self.path_label.setWordWrap(True)
        body.addWidget(self.path_label)

        # Период — главное поле: от него зависит каждая цифра, поэтому первым и
        # отдельной строкой, а сроки запаса — вторым рядом.
        period = QHBoxLayout()
        period.setSpacing(9)
        period.addWidget(QLabel("Период выгрузки:", card))
        self.start_input = DateInput(card)
        self.end_input = DateInput(card)
        period.addWidget(self.start_input)
        period.addWidget(QLabel("—", card))
        period.addWidget(self.end_input)
        self.span_label = QLabel("укажите обе даты — в файле периода нет", card)
        self.span_label.setObjectName("Hint")
        period.addWidget(self.span_label, 1)
        body.addLayout(period)
        self.start_input.textChanged.connect(self._period_changed)
        self.end_input.textChanged.connect(self._period_changed)

        terms = QHBoxLayout()
        terms.setSpacing(9)
        self.target_input = self._months(card, self.settings.restock_target)
        self.low_input = self._months(card, self.settings.restock_low)
        self.high_input = self._months(card, self.settings.restock_high)
        for text, widget in (("Заказывать на", self.target_input),
                             ("мес.   «Мало», если хватит меньше", self.low_input),
                             ("мес.   «Избыток», если больше", self.high_input)):
            terms.addWidget(QLabel(text, card))
            terms.addWidget(widget)
        terms.addWidget(QLabel("мес.", card))
        terms.addStretch(1)
        body.addLayout(terms)

        body.addWidget(Hint(
            "Нужен отчёт 1С по брендам: по каждому товару продажи и остатки на складах. "
            "Период в таком файле не записан — укажите тот, за который выгружены продажи. "
            "Запас = (остаток + товар в пути) ÷ продажи в месяц. Заказ добирает запас до "
            "выбранного срока. Пересчёт сбрасывает ваши правки количества.", card))
        return card

    def _months(self, parent: QWidget, value: float) -> DecimalInput:
        box = DecimalInput(parent)
        box.setRange(0.1, 60.0)
        box.setDecimals(1)
        box.setSingleStep(0.5)
        box.setValue(value)
        box.setMaximumWidth(86)
        return box

    def _result_card(self) -> Card:
        card = Card(self)
        body = card.body()

        tiles = QHBoxLayout()
        tiles.setSpacing(Metrics.GAP)
        self.tile_order = MetricTile("Позиций к заказу", Palette.PRIMARY, card)
        self.tile_qty = MetricTile("Штук к заказу", Palette.SUCCESS, card)
        self.tile_out = MetricTile("Нет в наличии, а продажи были", Palette.DANGER, card)
        self.tile_low = MetricTile("Запаса мало", Palette.WARNING, card)
        for tile in (self.tile_order, self.tile_qty, self.tile_out, self.tile_low):
            tiles.addWidget(tile, 1)
        body.addLayout(tiles)

        self.warning = Hint("", card)
        self.warning.setStyleSheet(f"color: {Palette.WARNING};")
        self.warning.setVisible(False)
        body.addWidget(self.warning)

        filters = QHBoxLayout()
        filters.setSpacing(9)
        self.brand_box = SelectBox(card)
        self.brand_box.addItem(ALL_BRANDS)
        self.state_box = SelectBox(card)
        for text in (TO_ORDER, ALL_STATES, *restock.STATES):
            self.state_box.addItem(text)
        self.search_input = QLineEdit(card)
        self.search_input.setPlaceholderText("Поиск по артикулу, штрихкоду, названию")
        self.search_input.setClearButtonEnabled(True)
        for widget in (self.brand_box, self.state_box):
            filters.addWidget(widget)
        filters.addWidget(self.search_input, 1)
        body.addLayout(filters)
        self.brand_box.currentIndexChanged.connect(self._brand_changed)
        self.state_box.currentIndexChanged.connect(self._fill_items)
        self.search_input.textChanged.connect(self._fill_items)

        self.views = QTabWidget(card)
        self.items_table = self._table(ITEM_COLUMNS)
        self.items_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.items_table.itemChanged.connect(self._edited)
        self.brands_table = self._table([
            "Бренд", "Товаров", "Остаток", "В пути", "Продажи", "Позиций к заказу",
            "Штук к заказу", "Нет в наличии", "Мало", "Избыток", "Нет продаж"])
        self.places_table = self._table([
            "Склад / магазин", "Остаток", "В пути", "Продажи", "Товаров с продажами",
            "Кончилось, а продажи были", "Хватит, мес."])
        self.views.addTab(self.items_table, "Товары")
        self.views.addTab(self.brands_table, "Бренды")
        self.views.addTab(self.places_table, "Склады и магазины")
        body.addWidget(self.views, 1)

        self.hint = Hint("Выберите файл, укажите период и нажмите «Рассчитать».", card)
        body.addWidget(self.hint)
        return card

    def _action(self, parent: QWidget, title: str, icon: str,
                handler: Callable[[], None]) -> QPushButton:
        button = QPushButton(title, parent)
        button.setIcon(icons.icon(icon))
        button.clicked.connect(handler)
        return button

    def _table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers), self)
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        return table

    # --- файл ------------------------------------------------------------------

    def pick(self) -> None:
        start = os.path.dirname(self._path) if self._path else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Отчёт по брендам", start, f"Выгрузка 1С ({' '.join(restock.EXTENSIONS)})")
        if path:
            self.choose(path)

    def choose(self, path: str) -> None:
        if self._busy:
            return
        self._path = path
        self.path_label.setText(path)
        self._plan = None
        self._stock = None
        self.save_button.setEnabled(False)
        self.calc_button.setEnabled(False)
        self._set_busy(True)
        self.hint.setText("Читаю отчёт…")
        run_task(lambda: restock.read(path), on_result=self._loaded, on_error=self._failed)

    def _loaded(self, stock: restock.Stock) -> None:
        self._stock = stock
        self._set_busy(False)
        self.brand_box.blockSignals(True)
        self.brand_box.clear()
        brands = stock.brands
        if len(brands) != 1:
            self.brand_box.addItem(ALL_BRANDS)
        for brand in brands:
            self.brand_box.addItem(brand)
        self.brand_box.blockSignals(False)
        self._clear_tables()
        self.hint.setText(
            f"Товаров: {len(stock.items)} · брендов: {len(brands)} · складов и магазинов: "
            f"{len(stock.places)}. Укажите период и нажмите «Рассчитать».")
        self._refresh_actions()

    def _failed(self, message: str) -> None:
        self._set_busy(False)
        self.hint.setText("Разобрать не удалось.")
        self.notify(message, ToastKind.ERROR)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.pick_button.setEnabled(not busy)
        self.calc_button.setEnabled(not busy and self._stock is not None)
        self.save_button.setEnabled(not busy and self._plan is not None)

    def _refresh_actions(self) -> None:
        self._set_busy(self._busy)

    # --- период и расчёт -------------------------------------------------------

    def _period_changed(self) -> None:
        start, end = self.start_input.value(), self.end_input.value()
        if start and end:
            if end < start:
                self.span_label.setText("конец периода раньше начала")
                return
            days = (end - start).days + 1
            self.span_label.setText(f"{days} дн. ≈ {days / AVG_MONTH:.1f} мес.")
        else:
            self.span_label.setText("укажите обе даты — в файле периода нет")

    def calculate(self) -> None:
        if self._stock is None or self._busy:
            return
        start, end = self.start_input.value(), self.end_input.value()
        if not start or not end:
            self.notify("Укажите период выгрузки: начало и конец, как в параметрах "
                        "отчёта 1С", ToastKind.WARNING)
            return
        params = restock.Params(start, end, self.target_input.value(),
                                self.low_input.value(), self.high_input.value())
        try:
            plan = restock.build(self._stock, params)
        except ValueError as error:
            self.notify(str(error), ToastKind.WARNING)
            return
        self.settings.restock_target = params.target
        self.settings.restock_low = params.low
        self.settings.restock_high = params.high
        self.settings.save()
        self._plan = plan
        self._refresh_actions()
        self._show()
        self.notify("Запас и заказ рассчитаны", ToastKind.SUCCESS)

    # --- показ -----------------------------------------------------------------

    def _brand(self) -> str | None:
        text = self.brand_box.currentText()
        return None if not text or text == ALL_BRANDS else text

    def _brand_changed(self) -> None:
        if self._plan is not None:
            self._show_tiles()
            self._fill_items()

    def _show(self) -> None:
        plan = self._plan
        if plan is None:
            return
        notes = list(plan.warnings) + list(plan.stock.warnings)
        if plan.stock.balanced is False:
            notes.insert(0, "Товары не сошлись с итогами файла — числам верить нельзя, "
                            "пришлите файл на разбор")
        self.warning.setText(" · ".join(notes))
        self.warning.setVisible(bool(notes))
        self._show_tiles()
        self._fill_items()
        self._fill_brands()
        self._fill_places()

    def _show_tiles(self) -> None:
        plan = self._plan
        if plan is None:
            return
        rows = plan.of_brand(self._brand())
        ordered = [row for row in rows if row.qty > 0]
        counts = plan.counts(rows)
        self.tile_order.set_value(len(ordered))
        self.tile_qty.set_value(_qty(sum(row.qty for row in ordered)) or "0")
        self.tile_out.set_value(counts[restock.OUT])
        self.tile_low.set_value(counts[restock.LOW])

    def _clear_tables(self) -> None:
        for table in (self.items_table, self.brands_table, self.places_table):
            table.setRowCount(0)
        self._shown = []
        self.warning.setVisible(False)
        for tile in (self.tile_order, self.tile_qty, self.tile_out, self.tile_low):
            tile.set_value(0)

    def _matches(self, row: restock.Row, state: str, words: list[str]) -> bool:
        if state == TO_ORDER:
            if row.qty <= 0:
                return False
        elif state != ALL_STATES and row.state != state:
            return False
        if not words:
            return True
        item = row.item
        blob = f"{item.article} {item.barcode} {item.name} {item.trait}".lower()
        return all(word in blob for word in words)

    def _fill_items(self) -> None:
        plan = self._plan
        if plan is None:
            return
        words = self.search_input.text().lower().split()
        state = self.state_box.currentText()
        shown = [row for row in plan.of_brand(self._brand())
                 if self._matches(row, state, words)]
        # Срочное первым, внутри состояния — что продаётся лучше.
        shown.sort(key=lambda row: (restock.STATES.index(row.state), -row.item.sales))
        self._shown = shown

        table = self.items_table
        self._filling = True
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(len(shown))
            for index, row in enumerate(shown):
                self._put_row(table, index, row)
            table.resizeColumnsToContents()
            table.setColumnWidth(NAME_COLUMN, 300)
        finally:
            table.setUpdatesEnabled(True)
            self._filling = False
        total = len(plan.of_brand(self._brand()))
        self.hint.setText(f"Показано {len(shown)} из {total}. Количество в колонке "
                          "«К заказу» можно править двойным щелчком.")

    def _put_row(self, table: QTableWidget, index: int, row: restock.Row) -> None:
        item = row.item
        values = {
            "Бренд": item.brand, "Артикул": item.article or item.barcode,
            "Товар": item.title, "Состояние": row.state, "К заказу": str(row.qty),
            "Остаток": _qty(item.stock), "В пути": _qty(item.transit),
            "Продажи": _qty(item.sales),
            "В месяц": _decimal(row.per_month) if row.per_month else "",
            "Хватит, мес.": _decimal(row.cover) if row.cover is not None else "",
            "Статус 1С": item.status, "1С советует": _qty(item.recommended),
        }
        for column, title in enumerate(ITEM_COLUMNS):
            text = values[title]
            cell = QTableWidgetItem(text)
            if title not in TEXT_COLUMNS:
                cell.setTextAlignment(RIGHT)
            if column == NAME_COLUMN:
                cell.setToolTip(text)
            if column != ORDER_COLUMN:
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if column == STATE_COLUMN and row.state in STATE_COLORS:
                cell.setForeground(QColor(STATE_COLORS[row.state]))
            if column == ORDER_COLUMN:
                self._style_order(cell, row)
            table.setItem(index, column, cell)

    def _style_order(self, cell: QTableWidgetItem, row: restock.Row) -> None:
        font = QFont(cell.font())
        font.setBold(row.qty > 0 or row.changed)
        cell.setFont(font)
        cell.setForeground(QColor(Palette.PRIMARY if row.changed else Palette.TEXT))
        cell.setToolTip(f"Расчёт: {row.order} шт — изменено вручную" if row.changed
                        else "Двойной щелчок — изменить количество")

    def _edited(self, cell: QTableWidgetItem) -> None:
        if self._filling or cell.column() != ORDER_COLUMN:
            return
        index = cell.row()
        if not 0 <= index < len(self._shown):
            return
        row = self._shown[index]
        text = cell.text().replace(" ", "").strip()
        try:
            value = int(float(text.replace(",", ".")))
            if value < 0:
                raise ValueError
        except ValueError:
            self.notify("Количество — целое число, не меньше нуля", ToastKind.WARNING)
            value = row.qty
        row.manual = None if value == row.order else value
        self._filling = True
        try:
            cell.setText(str(row.qty))
            self._style_order(cell, row)
        finally:
            self._filling = False
        self._show_tiles()
        self._fill_brands()

    def _fill_brands(self) -> None:
        plan = self._plan
        if plan is None:
            return
        groups = plan.brands()
        table = self.brands_table
        table.setRowCount(len(groups))
        for index, group in enumerate(groups):
            states = group.states
            values = [group.name, group.items, group.stock, group.transit, group.sales,
                      group.ordered_items, group.order, states.get(restock.OUT, 0),
                      states.get(restock.LOW, 0), states.get(restock.HIGH, 0),
                      states.get(restock.IDLE, 0)]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value if isinstance(value, str) else _qty(value))
                if column:
                    cell.setTextAlignment(RIGHT)
                table.setItem(index, column, cell)
        table.resizeColumnsToContents()

    def _fill_places(self) -> None:
        plan = self._plan
        if plan is None:
            return
        table = self.places_table
        table.setRowCount(len(plan.places))
        for index, place in enumerate(plan.places):
            values = [place.name, _qty(place.stock), _qty(place.transit),
                      _qty(place.sales) if place.selling else "",
                      _qty(place.sold_items) if place.selling else "",
                      _qty(place.missing_items) if place.selling else "",
                      _decimal(place.cover)]
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if column:
                    cell.setTextAlignment(RIGHT)
                if column == 5 and place.missing_items:
                    cell.setForeground(QColor(Palette.DANGER))
                table.setItem(index, column, cell)
        table.resizeColumnsToContents()

    # --- сохранение ------------------------------------------------------------

    def save(self) -> None:
        plan = self._plan
        if plan is None:
            return
        brand = self._brand()
        folder = (os.path.dirname(self._path) if self._path else "") or os.path.expanduser("~")
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить заказ", restock.default_name(folder, plan, brand),
            "Excel (*.xlsx)")
        if not path:
            return
        self._set_busy(True)
        run_task(lambda: restock.save(plan, path, brand),
                 on_result=self._saved, on_error=self._failed)

    def _saved(self, path: str) -> None:
        self._set_busy(False)
        self.notify(f"Сохранено: {os.path.basename(path)}", ToastKind.SUCCESS)
        self.hint.setText(f"Файл сохранён: {path}. Первый лист «К заказу» открывается во "
                          "вкладке «Заказ» как выгрузка 1С.")
