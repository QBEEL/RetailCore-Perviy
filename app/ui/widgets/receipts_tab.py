"""Вкладка «Аналитика по чекам»: выгрузка 1С → выручка, прибыль и срезы.

Порядок срезов — от крупного к мелкому: магазины, потом бренды и категории,
потом товары и продавцы, и в конце — когда продают (дни и часы), акции и
возвраты. Период выгрузки стоит над цифрами: выручка без периода ни о чём не
говорит, а в файле он записан в параметрах отчёта, и искать его глазами не
нужно.

Разбор уходит в фоновую задачу: пять тысяч строк читаются секунды, и замерший
на это время интерфейс выглядит как зависшая программа.
"""
from __future__ import annotations

import os
from typing import Any, Callable

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

from ...core import receipts
from ...core.receipts.analysis import SELF_SERVICE_TITLE, Group, weekday
from ...core.settings import AppSettings
from .. import icons
from ..tasks import run_task
from ..theme import Metrics, Palette
from .common import Card, Hint, MetricTile, SectionTitle
from .toast import ToastKind

RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

# Колонка среза: заголовок, значение и вид (t — текст, n — штуки, m — деньги,
# % — доля). Значение берётся из строки и отчёта: доля выручки нужна обоим.
Spec = tuple[str, Callable[[Any, "receipts.Report"], Any], str]


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") if value else ""


def _qty(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") if value else ""


def _share(value: float) -> str:
    return f"{value:.1%}" if value else ""


_SHOW = {"m": _money, "n": _qty, "%": _share}

# Общие колонки среза — одинаковые у магазинов, брендов, категорий и акций.
METRICS: list[Spec] = [
    ("Выручка", lambda g, r: g.revenue, "m"),
    ("Доля", lambda g, r: r.share(g), "%"),
    ("Прибыль", lambda g, r: g.profit, "m"),
    ("Маржа", lambda g, r: g.margin, "%"),
    ("Чеков", lambda g, r: g.checks, "n"),
    ("Средний чек", lambda g, r: g.average_check, "m"),
    ("Продано, шт", lambda g, r: g.quantity, "n"),
    ("Скидка", lambda g, r: g.discount_share, "%"),
    ("Возвраты", lambda g, r: g.returned, "m"),
]


def _named(title: str) -> list[Spec]:
    return [(title, lambda g, r: g.name, "t"), *METRICS]


class ReceiptsTab(QWidget):
    """Разбор выгрузки «Аналитика по чекам»."""

    def __init__(self, settings: AppSettings,
                 notify: Callable[[str, ToastKind], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._paths: list[str] = []
        self._report: receipts.Report | None = None
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
        header.addWidget(SectionTitle("Аналитика по чекам из 1С", card))
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
            "Подходит выгрузка «Аналитика по чекам» в Excel: строка на каждый "
            "товар в чеке. Период берётся из параметров отчёта над таблицей. "
            "Колонки ищутся по подписям, поэтому их порядок в настройке отчёта "
            "не важен. Можно выбрать несколько файлов — например, по месяцу, — "
            "чек, попавший в два файла, учтётся один раз.", card))
        return card

    def _result_card(self) -> Card:
        card = Card(self)
        body = card.body()

        self.period_title = SectionTitle("Период не выбран", card)
        body.addWidget(self.period_title)

        tiles = QHBoxLayout()
        tiles.setSpacing(Metrics.GAP)
        self.tile_revenue = MetricTile("Выручка", Palette.PRIMARY, card)
        self.tile_profit = MetricTile("Валовая прибыль", Palette.SUCCESS, card)
        self.tile_check = MetricTile("Средний чек", Palette.INFO, card)
        self.tile_discount = MetricTile("Скидки", Palette.WARNING, card)
        for tile in (self.tile_revenue, self.tile_profit, self.tile_check,
                     self.tile_discount):
            tiles.addWidget(tile, 1)
        body.addLayout(tiles)

        self.summary = Hint("", card)
        body.addWidget(self.summary)
        self.warning = Hint("", card)
        self.warning.setStyleSheet(f"color: {Palette.WARNING};")
        self.warning.setVisible(False)
        body.addWidget(self.warning)

        self.views = QTabWidget(card)
        self.tables: dict[str, QTableWidget] = {}
        for key, title in (("stores", "Магазины"), ("brands", "Бренды"),
                           ("categories", "Категории"), ("items", "Товары"),
                           ("sellers", "Продавцы"), ("days", "По дням"),
                           ("hours", "По часам"), ("promos", "Акции"),
                           ("returns", "Возвраты")):
            self.tables[key] = self._table()
            self.views.addTab(self.tables[key], title)
        body.addWidget(self.views, 1)

        self.hint = Hint("Выберите файл и нажмите «Разобрать».", card)
        body.addWidget(self.hint)
        return card

    def _action(self, parent: QWidget, title: str, icon: str,
                handler: Callable[[], None]) -> QPushButton:
        button = QPushButton(title, parent)
        button.setIcon(icons.icon(icon))
        button.clicked.connect(handler)
        return button

    def _table(self) -> QTableWidget:
        table = QTableWidget(0, 0, self)
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
            self, "Аналитика по чекам", start,
            f"Выгрузка 1С ({' '.join(receipts.EXTENSIONS)})")
        if paths:
            self.choose(paths)

    def choose(self, paths: list[str]) -> None:
        self._paths = list(paths)
        self.path_label.setText(
            paths[0] if len(paths) == 1 else
            f"Файлов: {len(paths)} — " + ", ".join(os.path.basename(p) for p in paths))
        self.parse_button.setEnabled(True)
        self.save_button.setEnabled(False)
        self.hint.setText("Нажмите «Разобрать».")

    def parse(self) -> None:
        if not self._paths or self._busy:
            return
        self._set_busy(True)
        self.hint.setText("Читаю выгрузку…")
        paths = list(self._paths)
        run_task(lambda: receipts.build(receipts.read_many(paths)),
                 on_result=self.show_report, on_error=self._failed)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.pick_button.setEnabled(not busy)
        self.parse_button.setEnabled(not busy and bool(self._paths))
        self.save_button.setEnabled(not busy and self._report is not None)

    def _failed(self, message: str) -> None:
        self._set_busy(False)
        self.hint.setText("Разобрать не удалось.")
        self.notify(message, ToastKind.ERROR)

    def show_report(self, report: receipts.Report) -> None:
        self._report = report
        self._set_busy(False)
        data = report.receipts
        total = report.total
        has_cost = data.has("cost")

        self.period_title.setText(f"Период: {data.period_title}"
                                  + (f" · {data.days} дн." if data.days else ""))
        self.tile_revenue.set_value(_money(total.revenue) or "0")
        self.tile_profit.set_value(
            f"{_money(total.profit)} · {total.margin:.0%}" if has_cost else "нет себестоимости")
        self.tile_check.set_value(_money(total.average_check) or "—")
        self.tile_discount.set_value(_share(total.discount_share) or "0 %")

        parts = [f"чеков: {total.checks}", f"продано: {_qty(total.quantity)} шт",
                 f"товаров в чеке: {total.per_check:.2f}"]
        if report.per_day:
            parts.append(f"в день: {_money(report.per_day)} ₽")
        if report.guests:
            parts.append(f"гостей с картой: {report.guests}, повторно: "
                         f"{report.repeat_guests}")
        if total.returned:
            parts.append(f"возвраты: {_money(total.returned)} ₽")
        self.summary.setText(" · ".join(parts))

        notes = list(data.warnings)
        if data.balanced is False:
            notes.insert(0, "Строки не сошлись с итогом файла — числам верить "
                            "нельзя, пришлите файл на разбор")
        self.warning.setText(" · ".join(notes))
        self.warning.setVisible(bool(notes))

        self._fill("stores", report, report.stores, _named("Магазин"))
        self._fill("brands", report, report.brands, _named("Бренд"))
        self._fill("categories", report, report.categories, _named("Категория"))
        self._fill("items", report, report.items, [
            ("Товар", lambda g, r: g.name, "t"), ("Бренд", lambda g, r: g.brand, "t"),
            *METRICS, ("Магазинов", lambda g, r: len(g.stores), "n")])
        sellers = [*report.sellers]
        if report.self_service.lines:
            sellers.append(report.self_service)
        self._fill("sellers", report, sellers, _named("Продавец"))
        self._fill("days", report, report.days, [
            ("Дата", lambda d, r: f"{d[0]:%d.%m.%Y}", "t"),
            ("День", lambda d, r: weekday(d[0]), "t"),
            ("Выручка", lambda d, r: d[1].revenue, "m"),
            ("Чеков", lambda d, r: d[1].checks, "n"),
            ("Средний чек", lambda d, r: d[1].average_check, "m"),
            ("Продано, шт", lambda d, r: d[1].quantity, "n")])
        checks = total.checks or 1
        self._fill("hours", report, report.hours, [
            ("Час", lambda h, r: f"{h[0]:02d}:00–{h[0]:02d}:59", "t"),
            ("Чеков", lambda h, r: h[1].checks, "n"),
            ("Доля чеков", lambda h, r: h[1].checks / checks, "%"),
            ("Выручка", lambda h, r: h[1].revenue, "m"),
            ("Средний чек", lambda h, r: h[1].average_check, "m")])
        self._fill("promos", report, report.promos, _named("Акция"))
        self._fill("returns", report, report.returns, [
            ("Дата", lambda l, r: f"{l.day:%d.%m.%Y}" if l.day else "", "t"),
            ("Магазин", lambda l, r: l.store, "t"), ("Товар", lambda l, r: l.item, "t"),
            ("Сумма", lambda l, r: -l.amount, "m"), ("Гость", lambda l, r: l.guest, "t"),
            ("Продавец", lambda l, r: l.seller, "t")])
        for key, base, count in (("promos", "Акции", len(report.promos)),
                                 ("returns", "Возвраты", len(report.returns))):
            self.views.setTabText(self.views.indexOf(self.tables[key]),
                                  f"{base} ({count})" if count else base)

        self.hint.setText(" · ".join(filter(None, [
            f"файлов: {len(data.sources)}" if len(data.sources) > 1 else "",
            f"строк: {len(data.lines)}",
            f"магазинов: {len(report.stores)}", f"брендов: {len(report.brands)}",
            f"товаров: {len(report.items)}"])))
        self.notify("Аналитика по чекам разобрана", ToastKind.SUCCESS)

    def _fill(self, key: str, report: receipts.Report, rows: list,
              specs: list[Spec]) -> None:
        table = self.tables[key]
        table.clear()
        table.setColumnCount(len(specs))
        table.setHorizontalHeaderLabels([title for title, _, _ in specs])
        table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            for column, (_, getter, kind) in enumerate(specs):
                value = getter(item, report)
                cell = QTableWidgetItem(_SHOW[kind](value) if kind in _SHOW else str(value))
                if kind in _SHOW:
                    cell.setTextAlignment(RIGHT)
                else:
                    # Название магазина или товара не влезает в колонку, когда
                    # числовых колонок девять: полный текст — в подсказке.
                    cell.setToolTip(cell.text())
                # Покупки без продавца — строкой в конце и приглушённо: это не
                # продавец, и соревноваться с людьми ей незачем.
                if isinstance(item, Group) and item.name == SELF_SERVICE_TITLE:
                    cell.setForeground(QColor(Palette.TEXT_MUTED))
                table.setItem(row, column, cell)
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

    # --- сохранение -------------------------------------------------------------

    def save(self) -> None:
        if self._report is None:
            return
        folder = ((os.path.dirname(self._paths[0]) if self._paths else "")
                  or os.path.expanduser("~"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить разбор", receipts.default_name(folder, self._report),
            "Excel (*.xlsx)")
        if not path:
            return
        report = self._report
        self._set_busy(True)
        run_task(lambda: receipts.save(report, path),
                 on_result=self._saved, on_error=self._failed)

    def _saved(self, path: str) -> None:
        self._set_busy(False)
        self.notify(f"Сохранено: {os.path.basename(path)}", ToastKind.SUCCESS)
        self.hint.setText(f"Файл сохранён: {path}")
