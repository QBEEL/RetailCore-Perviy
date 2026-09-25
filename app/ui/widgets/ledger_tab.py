"""Вкладка «Ведомость по складам»: выгрузка 1С → понятные срезы.

Порядок на вкладке повторяет порядок вопросов к такой ведомости: сначала чего не
хватило, потом что вообще расходится, потом по каждому магазину, и только затем
свод — он нужен, когда к цифре появились вопросы, а не с самого начала.

Разбор уходит в фоновую задачу: файл на шестьдесят товаров читается мгновенно, а
на десять тысяч — уже нет, и замерший интерфейс выглядит как зависшая программа.
"""
from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core import ledger
from ...core.settings import AppSettings
from .. import icons
from ..tasks import run_task
from ..theme import Metrics, Palette
from .common import Card, Hint, MetricTile, SectionTitle
from .toast import ToastKind

RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


def _qty(value: float) -> str:
    """Число в штуках. Пусто вместо нуля: ноль в свободной клетке только шумит."""
    if not value:
        return ""
    return f"{value:,.0f}".replace(",", " ")


def _share(value: float, shown: bool = True) -> str:
    """Доля. Пусто, если запас в выгрузке неполный и доля была бы выдуманной."""
    return f"{value:.0%}" if value and shown else ""


class LedgerTab(QWidget):
    """Разбор ведомости по товарам на складах."""

    def __init__(self, settings: AppSettings,
                 notify: Callable[[str, ToastKind], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._paths: list[str] = []
        self._report: ledger.Report | None = None
        self._busy = False
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
        header.addWidget(SectionTitle("Ведомость из 1С", card))
        header.addStretch(1)
        self.pick_button = self._action(card, "Выбрать файлы", "open", self.pick)
        self.parse_button = self._action(card, "Разобрать", "run", self.parse)
        self.parse_button.setObjectName("Primary")
        self.parse_button.setEnabled(False)
        self.save_button = self._action(card, "Сохранить в Excel", "export", self.save)
        self.save_button.setEnabled(False)
        for button in (self.pick_button, self.parse_button, self.save_button):
            header.addWidget(button)
        body.addLayout(header)

        self.path_label = QLabel("Файл не выбран", card)
        self.path_label.setObjectName("Path")
        self.path_label.setWordWrap(True)
        body.addWidget(self.path_label)

        body.addWidget(Hint(
            "Подходит выгрузка «Ведомость по товарам на складах» из 1С в Excel: "
            "склады в колонках, склад колонкой или группами строк, один склад "
            "на файл. Можно выбрать несколько файлов — например, по файлу от "
            "каждого магазина, — они сведутся в одну ведомость. Колонки ищутся "
            "по подписям в шапке, строки групп не считаются: их числа — итоги "
            "вложенных товаров.", card))
        return card

    def _result_card(self) -> Card:
        card = Card(self)
        body = card.body()

        tiles = QHBoxLayout()
        tiles.setSpacing(Metrics.GAP)
        self.tile_out = MetricTile("Расход, шт", Palette.PRIMARY, card)
        self.tile_short = MetricTile("Кончилось, а расход был", Palette.DANGER, card)
        self.tile_tight = MetricTile("Остатка меньше, чем ушло", Palette.WARNING, card)
        self.tile_transit = MetricTile("Лежит в пути, шт", Palette.INFO, card)
        for tile in (self.tile_out, self.tile_short, self.tile_tight,
                     self.tile_transit):
            tiles.addWidget(tile, 1)
        body.addLayout(tiles)

        self.warning = Hint("", card)
        self.warning.setStyleSheet(f"color: {Palette.WARNING};")
        self.warning.setVisible(False)
        body.addWidget(self.warning)

        self.views = QTabWidget(card)
        self.short_table = self._table(
            ["Артикул", "Товар", "Магазин", "Расход", "Остаток", "Ушло от запаса"])
        self.top_table = self._table(
            ["№", "Артикул", "Товар", "Ед.", "Расход", "Доля", "Остаток",
             "Магазинов", "Кончился в"])
        self.stores_table = self._table(
            ["Магазин", "Товар", "Расход", "Остаток", "Товаров с расходом",
             "Кончилось", "Ушло от запаса"])
        self.matrix_table = self._table(["Артикул", "Товар", "Ед."])
        self.errors_table = self._table(
            ["Артикул", "Товар", "Магазин", "Расход", "Остаток"])
        self.views.addTab(self.short_table, "Не хватило")
        self.views.addTab(self.top_table, "Топ товаров")
        self.views.addTab(self.stores_table, "По магазинам")
        self.views.addTab(self.matrix_table, "Свод")
        self.views.addTab(self.errors_table, "Ошибки учёта")
        body.addWidget(self.views, 1)

        self.hint = Hint("Выберите файл ведомости и нажмите «Разобрать».", card)
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

    # --- действия --------------------------------------------------------------

    def pick(self) -> None:
        start = (os.path.dirname(self._paths[0]) if self._paths
                 else os.path.expanduser("~"))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Ведомость по товарам на складах", start,
            f"Выгрузка 1С ({' '.join(ledger.EXTENSIONS)})")
        if not paths:
            return
        self._paths = paths
        if len(paths) == 1:
            self.path_label.setText(paths[0])
        else:
            self.path_label.setText(
                f"Файлов: {len(paths)} — "
                + ", ".join(os.path.basename(path) for path in paths))
        self.parse_button.setEnabled(True)
        self.save_button.setEnabled(False)
        self.hint.setText("Файлы выбраны — нажмите «Разобрать»." if len(paths) > 1
                          else "Файл выбран — нажмите «Разобрать».")

    def parse(self) -> None:
        if not self._paths or self._busy:
            return
        self._set_busy(True)
        self.hint.setText("Читаю выгрузку…")
        paths = list(self._paths)
        run_task(lambda: ledger.build(ledger.read_many(paths)),
                 on_result=self._show, on_error=self._failed)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.pick_button.setEnabled(not busy)
        self.parse_button.setEnabled(not busy and bool(self._paths))
        self.save_button.setEnabled(not busy and self._report is not None)

    def _failed(self, message: str) -> None:
        self._set_busy(False)
        self.hint.setText("Разобрать не удалось.")
        self.notify(message, ToastKind.ERROR)

    def _show(self, report: ledger.Report) -> None:
        self._report = report
        self._set_busy(False)

        total = report.total
        self.tile_out.set_value(_qty(total.outgoing) or "0")
        self.tile_short.set_value(len(report.shortages))
        self.tile_tight.set_value(len(report.tight))
        self.tile_transit.set_value(
            _qty(sum(line.move.closing for line in report.transit)) or "0")

        self._fill_short(report)
        self._fill_top(report)
        self._fill_stores(report)
        self._fill_matrix(report)
        self._fill_errors(report)
        self.views.setTabText(4, f"Ошибки учёта ({len(report.negative)})"
                              if report.negative else "Ошибки учёта")

        self._warn(report)
        parts = [report.ledger.title, report.ledger.layout]
        if len(report.ledger.sources) > 1:
            parts.append(f"файлов: {len(report.ledger.sources)}")
        parts += [f"магазинов: {len(report.ledger.shops)}",
                  f"товаров: {len(report.ledger.items)}"]
        if report.shares:
            parts.append(f"израсходовано от запаса: {total.share:.1%}")
        self.hint.setText(" · ".join(part for part in parts if part))
        self.notify("Ведомость разобрана", ToastKind.SUCCESS)

    def _warn(self, report: ledger.Report) -> None:
        notes = []
        if not report.ledger.balanced:
            # Самое важное предупреждение: сумма по складам не сошлась с итогом
            # самого файла, значит шапку прочитали неверно.
            notes.append(
                "Сумма по складам не сошлась с колонкой «Итого» в файле — "
                "числам верить нельзя, пришлите файл на разбор")
        notes.extend(report.ledger.warnings)
        if report.doubled:
            names = ", ".join(store.title for store in report.doubled)
            notes.append(f"Склады с одинаковым названием разведены номером: {names}")
        if report.negative:
            notes.append(
                f"Отрицательный остаток: {len(report.negative)} — это ошибка "
                "учёта, а не нехватка товара")
        self.warning.setText(" · ".join(notes))
        self.warning.setVisible(bool(notes))

    # --- наполнение таблиц ------------------------------------------------------

    def _put(self, table: QTableWidget, row: int, column: int, text: str, *,
             align=None, color: str = "") -> None:
        cell = QTableWidgetItem(text)
        if align is not None:
            cell.setTextAlignment(align)
        if color:
            cell.setForeground(QColor(color))
        table.setItem(row, column, cell)

    def _fill_short(self, report: ledger.Report) -> None:
        table = self.short_table
        table.setRowCount(len(report.shortages))
        for row, line in enumerate(report.shortages):
            self._put(table, row, 0, line.article)
            self._put(table, row, 1, line.name)
            self._put(table, row, 2, line.store)
            self._put(table, row, 3, _qty(line.move.outgoing), align=RIGHT,
                      color=Palette.DANGER)
            self._put(table, row, 4, _qty(line.move.closing), align=RIGHT)
            self._put(table, row, 5, _share(line.move.share, report.shares),
                      align=RIGHT)
        self._stretch(table, 1)

    def _fill_top(self, report: ledger.Report) -> None:
        table = self.top_table
        rows = report.top
        table.setRowCount(len(rows))
        for row, item in enumerate(rows, 0):
            self._put(table, row, 0, str(row + 1), align=RIGHT)
            self._put(table, row, 1, item.article)
            self._put(table, row, 2, item.name)
            self._put(table, row, 3, item.item.unit)
            self._put(table, row, 4, _qty(item.move.outgoing), align=RIGHT)
            self._put(table, row, 5, _share(item.share), align=RIGHT)
            self._put(table, row, 6, _qty(item.move.closing), align=RIGHT)
            self._put(table, row, 7, str(len(item.active)), align=RIGHT)
            self._put(table, row, 8, ", ".join(item.ran_out),
                      color=Palette.DANGER if item.ran_out else "")
        self._stretch(table, 2)

    def _fill_stores(self, report: ledger.Report) -> None:
        table = self.stores_table
        table.setRowCount(sum(1 + len(total.top) for total in report.stores))
        row = 0
        for total in report.stores:
            self._put(table, row, 0, total.store.title)
            self._put(table, row, 1, "— всего по магазину")
            self._put(table, row, 2, _qty(total.move.outgoing), align=RIGHT)
            self._put(table, row, 3, _qty(total.move.closing), align=RIGHT)
            self._put(table, row, 4, str(total.items), align=RIGHT)
            self._put(table, row, 5, str(total.ran_out) if total.ran_out else "",
                      align=RIGHT, color=Palette.DANGER if total.ran_out else "")
            self._put(table, row, 6, _share(total.move.share, report.shares),
                      align=RIGHT)
            for column in range(7):
                if cell := table.item(row, column):
                    font = cell.font()
                    font.setBold(True)
                    cell.setFont(font)
            row += 1
            for line in total.top:
                self._put(table, row, 1, line.name)
                self._put(table, row, 2, _qty(line.move.outgoing), align=RIGHT)
                self._put(table, row, 3, _qty(line.move.closing), align=RIGHT)
                self._put(table, row, 6, _share(line.move.share, report.shares),
                          align=RIGHT)
                row += 1
        self._stretch(table, 1)

    def _fill_matrix(self, report: ledger.Report) -> None:
        """Свод: на каждый магазин две колонки — расход и остаток."""
        shops = report.ledger.shops
        table = self.matrix_table
        headers = ["Артикул", "Товар", "Ед."]
        for store in shops:
            headers.extend([f"{store.title}\nрасход", f"{store.title}\nостаток"])
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setRowCount(len(report.items))
        for row, item in enumerate(report.items):
            self._put(table, row, 0, item.article)
            self._put(table, row, 1, item.name)
            self._put(table, row, 2, item.item.unit)
            for index, store in enumerate(shops):
                move = item.item.move(store.title)
                column = 3 + index * 2
                self._put(table, row, column, _qty(move.outgoing), align=RIGHT)
                self._put(table, row, column + 1, _qty(move.closing), align=RIGHT,
                          color=Palette.DANGER if move.ran_out else "")
        self._stretch(table, 1)

    def _fill_errors(self, report: ledger.Report) -> None:
        table = self.errors_table
        table.setRowCount(len(report.negative))
        for row, line in enumerate(report.negative):
            self._put(table, row, 0, line.article)
            self._put(table, row, 1, line.name)
            self._put(table, row, 2, line.store)
            self._put(table, row, 3, _qty(line.move.outgoing), align=RIGHT)
            self._put(table, row, 4, _qty(line.move.closing), align=RIGHT,
                      color=Palette.DANGER)
        self._stretch(table, 1)

    def _stretch(self, table: QTableWidget, column: int) -> None:
        table.resizeColumnsToContents()
        head = table.horizontalHeader()
        head.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)

    # --- сохранение -------------------------------------------------------------

    def save(self) -> None:
        if self._report is None:
            return
        folder = ((os.path.dirname(self._paths[0]) if self._paths else "")
                  or os.path.expanduser("~"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить разбор", ledger.default_name(folder),
            "Excel (*.xlsx)")
        if not path:
            return
        report = self._report
        self._set_busy(True)
        run_task(lambda: ledger.save(report, path),
                 on_result=self._saved, on_error=self._failed)

    def _saved(self, path: str) -> None:
        self._set_busy(False)
        self.notify(f"Сохранено: {os.path.basename(path)}", ToastKind.SUCCESS)
        self.hint.setText(f"Файл сохранён: {path}")
