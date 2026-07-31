"""Оплаты поставщикам: календарь, таблица, аналитика, бюджет.

Одна страница с четырьмя подвкладками, а не четыре вкладки в боковом меню:
верхний уровень навигации должен оставаться читаемым, а все четыре вида работают
с одной и той же выборкой.

Выборка одна намеренно. Если бы таблица и дашборд фильтровались по отдельности,
цифры на них расходились бы, и объяснить это пользователю было бы нечем.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.payments import (
    Budget,
    BudgetUse,
    Day,
    Filter,
    LEVEL_PRESETS,
    MONTHS,
    Payment,
    PaymentOrigin,
    PaymentStatus,
    STATUS_ORDER,
    Stats,
    SuggestionKind,
    analytics,
    planning,
    service,
    transport,
)
# Источник оплат выбирается на каждом вызове: общая база, если выполнен вход,
# иначе своя локальная. Имя `store` оставлено прежним — вызовов по нему
# полтора десятка, и переименование ничего бы не дало.
from ..core.payments import data as store
from ..core.settings import AppSettings
from ..core.workbook import write_sheet
from . import icons
from .tasks import run_task
from .theme import Metrics, Palette
from .widgets.calendar_grid import LevelLegend, MonthGrid, money
from .widgets.charts import ChartBox
from .widgets.common import Card, Divider, Hint, SectionTitle, Subtitle, Title
from .widgets.inputs import DecimalInput, SelectBox
from .widgets.payment_dialogs import (
    STATUS_COLORS,
    BudgetDialog,
    BulkEditDialog,
    DateInput,
    ImportDialog,
    PaymentDialog,
    RecipientLinkDialog,
)
from .widgets.login_dialog import ensure_session
from .widgets.table import ROLE_PAYLOAD, Column, DataTable
from .widgets.toast import ToastKind

Notify = Callable[[str, ToastKind], None]


class PaymentsPage(QWidget):
    """Страница оплат."""

    def __init__(
        self,
        settings: AppSettings,
        notify: Notify,
        parent: QWidget | None = None,
        show_supplier: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._show_supplier = show_supplier
        self.rows: list[Payment] = []
        self.stats = Stats()
        self._known: dict[str, list[str]] = {}
        self._budgets: dict[tuple[int, int], Budget] = {}
        self.selected_day: date | None = None
        today = date.today()
        self.year, self.month = today.year, today.month
        self._loaded = False

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 8, Metrics.PAD + 4, Metrics.PAD + 8, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(Title("Оплаты поставщикам", self))
        self.subtitle = Subtitle("История оплат, календарь, бюджет и планирование", self)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)

        self.import_button = QPushButton("Импорт из 1С", self)
        self.import_button.setIcon(icons.icon("open"))
        self.import_button.clicked.connect(self.run_import)
        head.addWidget(self.import_button)

        self.link_button = QPushButton("Привязать получателей", self)
        self.link_button.setIcon(icons.icon("link"))
        self.link_button.clicked.connect(self.link_recipients)
        head.addWidget(self.link_button)

        self.new_button = QPushButton("Новая оплата", self)
        self.new_button.setObjectName("Primary")
        self.new_button.setIcon(icons.icon("card"))
        self.new_button.clicked.connect(self.create_payment)
        head.addWidget(self.new_button)
        root.addLayout(head)

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._calendar_tab(), "Календарь")
        self.tabs.addTab(self._table_tab(), "Таблица")
        self.tabs.addTab(self._dashboard_tab(), "Аналитика")
        self.tabs.addTab(self._budget_tab(), "Бюджет")
        self.tabs.currentChanged.connect(self._tab_changed)
        root.addWidget(self.tabs, 1)

        self.empty = Hint(
            "Оплат пока нет. Нажмите «Импорт из 1С» и выберите выгрузку "
            "«Оплата поставщикам» — история загрузится целиком.", self)
        root.addWidget(self.empty)

    # =========================================================================
    # Календарь
    # =========================================================================

    def _calendar_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, Metrics.GAP, 0, 0)
        layout.setSpacing(Metrics.GAP)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        previous = QPushButton("‹", page)
        previous.setObjectName("Ghost")
        previous.setFixedWidth(34)
        previous.clicked.connect(lambda: self.shift_month(-1))
        bar.addWidget(previous)

        self.month_label = QLabel("", page)
        self.month_label.setObjectName("SectionTitle")
        self.month_label.setMinimumWidth(170)
        bar.addWidget(self.month_label)

        forward = QPushButton("›", page)
        forward.setObjectName("Ghost")
        forward.setFixedWidth(34)
        forward.clicked.connect(lambda: self.shift_month(1))
        bar.addWidget(forward)

        today_button = QPushButton("Сегодня", page)
        today_button.setObjectName("Ghost")
        today_button.clicked.connect(self.show_today)
        bar.addWidget(today_button)

        bar.addSpacing(Metrics.GAP)
        self.legend = LevelLegend(page)
        bar.addWidget(self.legend)
        bar.addStretch(1)

        self.month_summary = QLabel("", page)
        self.month_summary.setObjectName("Hint")
        bar.addWidget(self.month_summary)
        layout.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal, page)
        self.grid = MonthGrid(split)
        self.grid.day_clicked.connect(self.show_day)
        self.grid.payment_moved.connect(self.move_payment)
        split.addWidget(self.grid)

        side = Card(split)
        body = side.body()
        self.day_title = SectionTitle("Выберите день", side)
        body.addWidget(self.day_title)
        self.day_summary = Hint("", side)
        body.addWidget(self.day_summary)
        body.addWidget(Divider(side))
        self.day_list = QListWidget(side)
        self.day_list.itemDoubleClicked.connect(self._open_from_day)
        body.addWidget(self.day_list, 1)
        self.day_add = QPushButton("Оплата на этот день", side)
        self.day_add.setIcon(icons.icon("card"))
        self.day_add.setEnabled(False)
        self.day_add.clicked.connect(self._create_on_day)
        body.addWidget(self.day_add)
        split.addWidget(side)
        split.setSizes([760, 340])
        layout.addWidget(split, 1)

        self.budget_line = Hint("", page)
        layout.addWidget(self.budget_line)
        return page

    def shift_month(self, step: int) -> None:
        month = self.month + step
        year = self.year + (month - 1) // 12
        self.month = (month - 1) % 12 + 1
        self.year = year
        self.refresh_calendar()

    def show_today(self) -> None:
        today = date.today()
        self.year, self.month = today.year, today.month
        self.refresh_calendar()

    def refresh_calendar(self) -> None:
        levels = self.settings.day_levels
        days = analytics.days_of(self.rows, self.year, self.month)
        self.grid.show_month(self.year, self.month, days, levels=levels, today=date.today())
        self.legend.show_levels(levels)
        self.month_label.setText(f"{MONTHS[self.month - 1]} {self.year}")

        total = sum(day.total for day in days.values())
        count = sum(day.count for day in days.values())
        overdue = sum(day.overdue for day in days.values())
        parts = [f"платежей: {count}", f"сумма: {money(total)} ₽"]
        if overdue:
            parts.append(f"просрочено: {overdue}")
        self.month_summary.setText(" · ".join(parts))
        self._refresh_budget_line()

    def _refresh_budget_line(self) -> None:
        use = self._budget_use(self.year, self.month)
        if use.budget.amount <= 0:
            self.budget_line.setText(
                f"Бюджет на {MONTHS[self.month - 1].lower()} не задан — "
                "его можно установить на вкладке «Бюджет».")
            return
        colour = Palette.DANGER if use.over else (
            Palette.WARNING if use.near(self.settings.payment_budget_warn) else Palette.TEXT_MUTED)
        text = (
            f"Бюджет {money(use.budget.amount)} ₽ · использовано {money(use.total)} ₽"
            f" ({use.percent:.0f} %) · остаток {money(use.left)} ₽")
        if use.over:
            text += f"  —  превышение на {money(-use.left)} ₽"
        self.budget_line.setText(text)
        self.budget_line.setStyleSheet(f"color: {colour}; font-size: 12px;")

    def show_day(self, day: Day) -> None:
        self.selected_day = day.day
        self.day_add.setEnabled(True)
        self.day_title.setText(f"{day.day:%d.%m.%Y}, {_weekday(day.day)}")
        if not day.count:
            self.day_summary.setText("оплат нет")
            self.day_list.clear()
            return
        parts = [f"платежей: {day.count}", f"сумма: {money(day.total)} ₽"]
        if day.overdue:
            parts.append(f"просрочено: {day.overdue}")
        self.day_summary.setText(" · ".join(parts))
        self.day_list.clear()
        for payment in day.payments:
            item = QListWidgetItem(
                f"{money(payment.amount)} ₽   {payment.title}\n{payment.status.title}"
                + (f" · {payment.responsible}" if payment.responsible else ""))
            item.setData(Qt.ItemDataRole.UserRole, payment.id)
            item.setForeground(QColor(STATUS_COLORS[payment.status]))
            self.day_list.addItem(item)

    def _open_from_day(self, item: QListWidgetItem) -> None:
        payment_id = int(item.data(Qt.ItemDataRole.UserRole) or 0)
        if payment := next((p for p in self.rows if p.id == payment_id), None):
            self.open_payment(payment)

    def _create_on_day(self) -> None:
        self.create_payment(pay_date=self.selected_day or date.today())

    def move_payment(self, payment: Payment, day: date) -> None:
        """Перенос платежа на другой день перетаскиванием."""
        if not payment.status.open:
            self.notify("Перенести можно только неоплаченный платёж", ToastKind.WARNING)
            return
        was = payment.pay_date
        payment.pay_date = day
        # Перенос — осознанное решение человека, и статус это фиксирует: иначе
        # платёж, сдвинутый в прошлое, тут же стал бы просрочкой.
        payment.status = PaymentStatus.MOVED
        try:
            store.save_payment(payment)
        except ValueError as failure:
            payment.pay_date = was
            self.notify(str(failure), ToastKind.ERROR)
            return
        self.notify(
            f"{payment.title}: перенесено на {day:%d.%m.%Y}", ToastKind.SUCCESS)
        self.reload()

    # =========================================================================
    # Таблица
    # =========================================================================

    def _table_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, Metrics.GAP, 0, 0)
        layout.setSpacing(Metrics.GAP)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.search = QLineEdit(page)
        self.search.setPlaceholderText("Поиск: поставщик, номер заявки, комментарий, ответственный")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_table)
        top.addWidget(self.search, 1)

        self.status_filter = SelectBox(page)
        self.status_filter.addItem("Все статусы", "")
        for status in STATUS_ORDER:
            self.status_filter.addItem(status.title, status.value)
        self.status_filter.currentIndexChanged.connect(self.reload)
        top.addWidget(self.status_filter)

        self.period_filter = SelectBox(page)
        for label, days in (
            ("Всё время", 0), ("Текущий месяц", -1), ("30 дней", 30),
            ("90 дней", 90), ("Год", 365), ("Будущие", -2),
        ):
            self.period_filter.addItem(label, days)
        self.period_filter.currentIndexChanged.connect(self.reload)
        top.addWidget(self.period_filter)
        layout.addLayout(top)

        second = QHBoxLayout()
        second.setSpacing(8)
        second.addWidget(QLabel("Сумма от", page))
        self.amount_from = DecimalInput(page)
        self.amount_from.setRange(0.0, 1_000_000_000.0)
        self.amount_from.setDecimals(0)
        self.amount_from.setGroupSeparatorShown(True)
        self.amount_from.setMaximumWidth(140)
        second.addWidget(self.amount_from)
        second.addWidget(QLabel("до", page))
        self.amount_to = DecimalInput(page)
        self.amount_to.setRange(0.0, 1_000_000_000.0)
        self.amount_to.setDecimals(0)
        self.amount_to.setGroupSeparatorShown(True)
        self.amount_to.setMaximumWidth(140)
        second.addWidget(self.amount_to)

        self.responsible_filter = SelectBox(page)
        self.responsible_filter.addItem("Все ответственные", "")
        self.responsible_filter.setMinimumWidth(180)
        second.addWidget(self.responsible_filter)

        self.operation_filter = SelectBox(page)
        self.operation_filter.addItem("Все операции", "")
        self.operation_filter.setMinimumWidth(180)
        second.addWidget(self.operation_filter)

        apply_button = QPushButton("Применить", page)
        apply_button.clicked.connect(self.reload)
        second.addWidget(apply_button)
        reset = QPushButton("Сбросить", page)
        reset.setObjectName("Ghost")
        reset.setIcon(icons.icon("reset"))
        reset.clicked.connect(self.reset_filters)
        second.addWidget(reset)
        second.addStretch(1)
        layout.addLayout(second)

        self.table = DataTable(self._columns(), page)
        self.table.item_activated.connect(self.open_payment)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.table_summary = Hint("", page)
        bottom.addWidget(self.table_summary, 1)
        self.bulk_button = QPushButton("Изменить выделенные", page)
        self.bulk_button.setIcon(icons.icon("columns"))
        self.bulk_button.setEnabled(False)
        self.bulk_button.clicked.connect(self.bulk_edit)
        bottom.addWidget(self.bulk_button)
        self.delete_button = QPushButton("Удалить", page)
        self.delete_button.setObjectName("Danger")
        self.delete_button.setIcon(icons.icon("trash"))
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self.delete_selected)
        bottom.addWidget(self.delete_button)
        export = QPushButton("Экспорт в Excel", page)
        export.setIcon(icons.icon("export"))
        export.clicked.connect(self.export)
        bottom.addWidget(export)
        layout.addLayout(bottom)
        return page

    def _columns(self) -> list[Column]:
        return [
            Column("Дата", lambda p: f"{p.pay_date:%d.%m.%Y}" if p.pay_date else "—",
                   width=96, sort_key=lambda p: p.pay_date or date.min),
            Column("Поставщик", lambda p: p.title, width=250, highlight=True),
            Column("Сумма, ₽", lambda p: money(p.amount), width=130,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda p: p.amount),
            Column("Статус", lambda p: p.status.title, width=126,
                   color=lambda p: QColor(STATUS_COLORS[p.status]),
                   sort_key=lambda p: STATUS_ORDER.index(p.status)),
            Column("Заявка", lambda p: p.doc_number, width=118, highlight=True),
            Column("Дата заявки", lambda p: f"{p.request_date:%d.%m.%Y}" if p.request_date else "",
                   width=104, sort_key=lambda p: p.request_date or date.min),
            Column("Отсрочка", lambda p: _terms_text(p), width=88,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda p: _terms_days(p)),
            Column("Ответственный", lambda p: p.responsible, width=170, highlight=True),
            Column("Операция", lambda p: p.operation, width=170),
            Column("НДС, ₽", lambda p: money(p.vat) if p.vat else "", width=110,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda p: p.vat),
            Column("Комментарий", lambda p: p.comment, width=200, highlight=True),
            Column("Документы", lambda p: _files_text(p), width=100),
            Column("Источник", lambda p: p.origin.title, width=130),
        ]

    def _filter_table(self, text: str) -> None:
        self.table.proxy.set_text(text)
        self.table.model_.set_terms([t for t in text.split() if len(t) > 1])
        self._update_table_summary()

    def _selection_changed(self) -> None:
        count = len(self.table.selected_items())
        self.bulk_button.setEnabled(count > 0)
        self.delete_button.setEnabled(count > 0)
        self.bulk_button.setText(
            f"Изменить выделенные ({count})" if count else "Изменить выделенные")

    def _update_table_summary(self) -> None:
        """Итог под таблицей. Отменённое в сумму не входит — как и во всех сводках.

        Иначе на одном экране получались бы две несходящиеся суммы: 102
        отклонённые заявки на 25 млн, посчитанные здесь и не посчитанные в
        объёме выборки.
        """
        shown = self.table.proxy.rowCount()
        payments = [
            payment for row in range(shown)
            if (payment := self.table.proxy.index(row, 0).data(ROLE_PAYLOAD)) is not None
        ]
        cancelled = [p for p in payments if p.status is PaymentStatus.CANCELLED]
        total = sum(p.amount for p in payments if p.status is not PaymentStatus.CANCELLED)
        text = f"показано {shown} из {len(self.rows)} · сумма показанных {money(total)} ₽"
        if cancelled:
            text += f" · отменённых {len(cancelled)} на {money(sum(p.amount for p in cancelled))} ₽"
        self.table_summary.setText(text)

    def reset_filters(self) -> None:
        self.search.clear()
        self.status_filter.setCurrentIndex(0)
        self.period_filter.setCurrentIndex(0)
        self.amount_from.setValue(0)
        self.amount_to.setValue(0)
        self.responsible_filter.setCurrentIndex(0)
        self.operation_filter.setCurrentIndex(0)
        self.reload()

    def bulk_edit(self) -> None:
        selected = [p for p in self.table.selected_items() if p is not None]
        if not selected:
            return
        dialog = BulkEditDialog(len(selected), self._known.get("responsible", []), self)
        if not dialog.exec():
            return
        changes = dialog.changes()
        if not changes:
            self.notify("Ничего не изменилось: все поля оставлены пустыми", ToastKind.INFO)
            return
        count = store.update_many([p.id for p in selected], **changes)
        self.notify(f"Изменено оплат: {count}", ToastKind.SUCCESS)
        self.reload()

    def delete_selected(self) -> None:
        selected = [p for p in self.table.selected_items() if p is not None]
        if not selected:
            return
        answer = QMessageBox.question(
            self, "Удалить оплаты",
            f"Удалить {len(selected)} оплат безвозвратно?\n"
            "Импортированные записи вернутся при следующем импорте выгрузки.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = sum(1 for payment in selected if store.delete_payment(payment.id))
        self.notify(f"Удалено оплат: {removed}", ToastKind.SUCCESS)
        self.reload()

    def export(self) -> None:
        """Выгружает показанные строки в Excel."""
        shown = [
            self.table.proxy.index(row, 0).data(ROLE_PAYLOAD)
            for row in range(self.table.proxy.rowCount())
        ]
        shown = [p for p in shown if p is not None]
        if not shown:
            self.notify("Нечего экспортировать: под фильтр не попала ни одна оплата",
                        ToastKind.WARNING)
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить таблицу оплат", "Оплаты.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        titles = [column.title for column in self.table.model_.columns]
        values = [[column.getter(payment) for column in self.table.model_.columns]
                  for payment in shown]
        # Суммы выгружаются числами, иначе в Excel по ним не посчитать итог.
        money_columns = {index for index, column in enumerate(titles)
                         if column in ("Сумма, ₽", "НДС, ₽")}
        for row, payment in zip(values, shown):
            for index in money_columns:
                row[index] = payment.amount if titles[index] == "Сумма, ₽" else payment.vat
        run_task(
            write_sheet, path, "Оплаты", titles, values,
            on_result=lambda _: self.notify(
                f"Сохранено оплат: {len(shown)}", ToastKind.SUCCESS),
            on_error=lambda message: self.notify(message, ToastKind.ERROR))

    # =========================================================================
    # Аналитика
    # =========================================================================

    def _dashboard_tab(self) -> QWidget:
        page = QWidget(self)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, Metrics.GAP, 0, 0)
        outer.setSpacing(Metrics.GAP)

        area = QScrollArea(page)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget(area)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Metrics.GAP)

        self.tiles: dict[str, QLabel] = {}
        tiles_card = Card(holder)
        tiles_grid = QGridLayout()
        tiles_grid.setHorizontalSpacing(Metrics.GAP + 8)
        tiles_grid.setVerticalSpacing(2)
        captions = [
            ("budget", "Бюджет месяца", Palette.TEXT_MUTED),
            ("paid", "Оплачено", Palette.SUCCESS),
            ("left", "Осталось оплатить", Palette.PRIMARY),
            ("overdue", "Просрочено", Palette.DANGER),
            ("count", "Оплат", Palette.TEXT_MUTED),
            ("average", "Средний платёж", Palette.TEXT_MUTED),
            ("biggest", "Крупнейший платёж", Palette.INFO),
            ("busiest", "Загруженный день", Palette.WARNING),
        ]
        for index, (key, label, colour) in enumerate(captions):
            value = QLabel("—", holder)
            value.setStyleSheet(f"font-size: 18px; font-weight: 600; color: {colour};")
            caption = QLabel(label, holder)
            caption.setObjectName("MetricLabel")
            tiles_grid.addWidget(value, (index // 4) * 2, index % 4)
            tiles_grid.addWidget(caption, (index // 4) * 2 + 1, index % 4)
            self.tiles[key] = value
        tiles_card.body().addLayout(tiles_grid)
        layout.addWidget(tiles_card)

        self.dashboard_hint = Hint("", holder)
        layout.addWidget(self.dashboard_hint)

        charts = QGridLayout()
        charts.setHorizontalSpacing(Metrics.GAP)
        charts.setVerticalSpacing(Metrics.GAP)
        self.chart_days = ChartBox("Расходы по дням (последние 45 дней)", holder)
        self.chart_suppliers = ChartBox("Расходы по поставщикам (топ-10)", holder)
        self.chart_months = ChartBox("Расходы по месяцам", holder)
        self.chart_budget = ChartBox("Распределение бюджета месяца", holder)
        # Месячный график занимает обе колонки: двадцать четыре подписи вида
        # «ноя 24» в половину ширины не помещаются и обрезаются.
        places = (
            (self.chart_days, 0, 0, 1, 1),
            (self.chart_suppliers, 0, 1, 1, 1),
            (self.chart_months, 1, 0, 1, 2),
            (self.chart_budget, 2, 0, 1, 2),
        )
        for box, row, column, rows_span, columns_span in places:
            card = Card(holder)
            card.body().addWidget(box)
            charts.addWidget(card, row, column, rows_span, columns_span)
        # График — не картинка, а способ добраться до строк.
        self.chart_days.activated.connect(self._chart_day)
        self.chart_months.activated.connect(self._chart_month)
        self.chart_suppliers.activated.connect(self._chart_recipient)
        self.chart_budget.activated.connect(self._chart_recipient)
        charts.setColumnStretch(0, 1)
        charts.setColumnStretch(1, 1)
        layout.addLayout(charts)

        plans = Card(holder)
        plans_head = QHBoxLayout()
        plans_head.addWidget(SectionTitle("Подсказки по истории", plans))
        plans_head.addStretch(1)
        self.plan_hint = Hint("", plans)
        plans_head.addWidget(self.plan_hint)
        plans.body().addLayout(plans_head)
        plans.body().addWidget(Hint(
            "Предложения строятся по ритму оплат каждого поставщика. Ничего не "
            "создаётся само — двойной щелчок открывает заполненную карточку.", plans))
        self.plans = QListWidget(plans)
        self.plans.setMinimumHeight(200)
        self.plans.itemDoubleClicked.connect(self._create_from_plan)
        plans.body().addWidget(self.plans)
        layout.addWidget(plans)

        rating = Card(holder)
        rating.body().addWidget(SectionTitle("Рейтинг поставщиков", rating))
        self.rating = DataTable(self._rating_columns(), rating)
        self.rating.setMinimumHeight(240)
        self.rating.item_activated.connect(self._open_supplier_from_rating)
        rating.body().addWidget(self.rating)
        layout.addWidget(rating)

        layout.addStretch(1)
        area.setWidget(holder)
        outer.addWidget(area, 1)
        return page

    def _rating_columns(self) -> list[Column]:
        return [
            Column("Поставщик", lambda s: s.title, width=260, highlight=True),
            Column("Всего, ₽", lambda s: money(s.total), width=140,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda s: s.total),
            Column("Оплат", lambda s: s.count, width=78,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda s: s.count),
            Column("Средняя, ₽", lambda s: money(s.average), width=130,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda s: s.average),
            Column("Максимум, ₽", lambda s: money(s.maximum), width=130,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda s: s.maximum),
            Column("Минимум, ₽", lambda s: money(s.minimum), width=120,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda s: s.minimum),
            Column("Ритм, дн", lambda s: f"{s.median_interval:.0f}" if s.median_interval else "",
                   width=90, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda s: s.median_interval),
            Column("Отсрочка, дн", lambda s: f"{s.median_terms:.0f}" if s.median_terms else "",
                   width=110, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda s: s.median_terms),
            Column("Обычно", lambda s: f"{s.common_day}-го ({s.day_share:.0f} %)"
                   if s.common_day else "", width=120, sort_key=lambda s: s.day_share),
            Column("Последняя", lambda s: f"{s.last_pay:%d.%m.%Y}" if s.last_pay else "",
                   width=110, sort_key=lambda s: s.last_pay or date.min),
            Column("Тишина, дн", lambda s: s.silent_days(), width=100,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda s: s.silent_days()),
        ]

    def refresh_dashboard(self) -> None:
        today = date.today()
        use = self._budget_use(today.year, today.month)
        month_rows = [
            p for p in self.rows
            if p.pay_date and p.pay_date.year == today.year and p.pay_date.month == today.month
        ]
        month_stats = analytics.overview(month_rows)

        self.tiles["budget"].setText(
            f"{money(use.budget.amount)} ₽" if use.budget.amount else "не задан")
        self.tiles["paid"].setText(f"{money(month_stats.paid)} ₽")
        self.tiles["left"].setText(f"{money(month_stats.planned + month_stats.overdue)} ₽")
        self.tiles["overdue"].setText(
            f"{money(self.stats.overdue)} ₽" if self.stats.overdue else "нет")
        self.tiles["count"].setText(str(self.stats.count))
        self.tiles["average"].setText(f"{money(self.stats.average)} ₽")
        self.tiles["biggest"].setText(
            f"{money(self.stats.maximum)} ₽" if self.stats.maximum else "—")
        if self.stats.busiest_day is not None:
            day = self.stats.busiest_day
            self.tiles["busiest"].setText(f"{day.day:%d.%m.%Y}")
            self.tiles["busiest"].setToolTip(
                f"{money(day.total)} ₽ · платежей {day.count}")
        else:
            self.tiles["busiest"].setText("—")

        hints: list[str] = [
            f"Всего в выборке: {money(self.stats.total)} ₽ за {self.stats.count} оплат",
            f"типичный ритм поставщика: {analytics.typical_interval(self.rows):.0f} дн",
            f"медианная отсрочка: {analytics.median_terms(self.rows):.0f} дн",
        ]
        if dateless := [p for p in self.rows if p.pay_date is None and p.status.open]:
            hints.append(
                f"без даты платежа: {len(dateless)} на {money(sum(p.amount for p in dateless))} ₽"
                " — в календарь и бюджет они не попадают")
        self.dashboard_hint.setText(" · ".join(hints))

        self._refresh_charts(month_stats, use)
        self._refresh_plans()
        self.rating.set_items(analytics.by_supplier(self.rows, limit=200))

    def _refresh_charts(self, month_stats: Stats, use: BudgetUse) -> None:
        """Четыре графика по той же выборке. Щелчок по любому ведёт к строкам."""
        today = date.today()

        # Непрерывный ряд вокруг сегодня, а не «последние дни выборки»: данные
        # уходят в будущее на три месяца, и отрезок от конца давал семь
        # разрозненных столбцов при подписи «45 дней».
        window = analytics.daily_window(self.rows, back=30, ahead=14, today=today)
        first, last = window[0][0], window[-1][0]
        self.chart_days.caption.setText(
            f"Расходы по дням · {first:%d.%m} — {last:%d.%m} (сегодня {today:%d.%m})")
        # Подписи оси скрыты: сорок пять дней Qt всё равно обрезает до точек.
        # Отрезок назван в заголовке, точная дата — в подсказке при наведении.
        self.chart_days.show_split_bars(
            [f"{day.day:02d}" for day, _, _, _ in window],
            [paid for _, paid, _, _ in window],
            [planned for _, _, planned, _ in window],
            labels_visible=False,
            payload=[day for day, _, _, _ in window],
            hints=[
                _day_hint(day, paid, planned, count, today)
                for day, paid, planned, count in window
            ])

        top = analytics.by_supplier(self.rows, limit=10)
        self.chart_suppliers.show_horizontal(
            [s.title for s in top], [s.total for s in top], color=Palette.INFO,
            payload=[s.recipient for s in top],
            hints=[
                f"{s.title}\n{money(s.total)} ₽ за {s.count} оплат"
                f"\nсредняя {money(s.average)} ₽"
                + (f"\nобычно каждые {s.median_interval:.0f} дн" if s.median_interval else "")
                + "\n\nщелчок — показать в таблице"
                for s in top
            ])

        # Двенадцать месяцев, а не двадцать четыре: `QBarCategoryAxis` отводит
        # подписи около сорока процентов слота, и на двух годах «ноя 24»
        # обрезается в «но...». История за все годы видна на вкладке «Бюджет».
        months = analytics.by_month(self.rows)[-12:]
        self.chart_months.caption.setText("Расходы по месяцам · последние 12")
        self.chart_months.show_bars(
            [f"{MONTHS[p.start.month - 1][:3].lower()} {p.start.year % 100:02d}" for p in months],
            [p.total for p in months],
            color=Palette.SUCCESS,
            payload=[p.start for p in months],
            hints=[
                f"{p.label}\n{money(p.total)} ₽ за {p.count} оплат"
                + (f"\n{p.change:+.0f} % к предыдущему" if p.previous else "")
                + "\n\nщелчок — открыть месяц в календаре"
                for p in months
            ])

        if use.budget.amount > 0:
            left = max(use.budget.amount - use.total, 0.0)
            self.chart_budget.caption.setText(
                f"Бюджет {MONTHS[today.month - 1].lower()}: {use.percent:.0f} % использовано")
            self.chart_budget.show_pie(
                ["Оплачено", "Предстоит", "Остаток бюджета"],
                [use.spent, use.planned, left])
        else:
            # Бюджета нет — показываем структуру расходов месяца по поставщикам.
            month_top = analytics.by_supplier(
                [p for p in self.rows if p.pay_date
                 and (p.pay_date.year, p.pay_date.month) == (today.year, today.month)],
                limit=12)
            self.chart_budget.caption.setText("Расходы месяца по поставщикам (бюджет не задан)")
            self.chart_budget.show_pie(
                [s.title for s in month_top], [s.total for s in month_top],
                payload=[s.recipient for s in month_top])

    def _chart_day(self, day: object) -> None:
        """Щелчок по столбцу дня — открыть этот день в календаре."""
        if not isinstance(day, date):
            return
        self.tabs.setCurrentIndex(0)
        self.year, self.month = day.year, day.month
        self.refresh_calendar()
        cell = analytics.days_of(self.rows, day.year, day.month).get(day)
        self.show_day(cell if cell is not None else Day(day=day))

    def _chart_month(self, start: object) -> None:
        """Щелчок по столбцу месяца — перевести календарь на него."""
        if not isinstance(start, date):
            return
        self.tabs.setCurrentIndex(0)
        self.year, self.month = start.year, start.month
        self.refresh_calendar()

    def _chart_recipient(self, recipient: object) -> None:
        """Щелчок по поставщику — отобрать его платежи в таблице."""
        if not recipient:
            return
        self.tabs.setCurrentIndex(1)
        self.search.setText(str(recipient))

    def _refresh_plans(self) -> None:
        found = planning.suggestions(self.rows, limit=40)
        self.plans.clear()
        urgent = sum(1 for item in found if item.urgent)
        self.plan_hint.setText(
            f"предложений: {len(found)}"
            + (f" · требуют внимания: {urgent}" if urgent else ""))
        for suggestion in found:
            mark = "⚠ " if suggestion.urgent else ""
            item = QListWidgetItem(
                f"{mark}{suggestion.stats.title}\n"
                f"{suggestion.kind.title} · {suggestion.reason}\n"
                f"предлагается: {suggestion.pay_date:%d.%m.%Y}"
                f" на {money(suggestion.amount)} ₽")
            item.setData(Qt.ItemDataRole.UserRole, suggestion)
            if suggestion.urgent:
                item.setForeground(QColor(Palette.DANGER))
            self.plans.addItem(item)

    def _create_from_plan(self, item: QListWidgetItem) -> None:
        suggestion = item.data(Qt.ItemDataRole.UserRole)
        if suggestion is None:
            return
        self.create_payment(
            recipient=suggestion.stats.recipient,
            amount=suggestion.amount,
            pay_date=suggestion.pay_date,
            supplier_id=suggestion.stats.supplier_id)

    def _open_supplier_from_rating(self, stats) -> None:  # type: ignore[no-untyped-def]
        if stats is None:
            return
        if stats.supplier_id and self._show_supplier is not None:
            self._show_supplier(stats.supplier_id)
            return
        self.tabs.setCurrentIndex(1)
        self.search.setText(stats.recipient)

    # =========================================================================
    # Бюджет
    # =========================================================================

    def _budget_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, Metrics.GAP, 0, 0)
        layout.setSpacing(Metrics.GAP)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.budget_year = SelectBox(page)
        self.budget_year.currentIndexChanged.connect(self.refresh_budget)
        bar.addWidget(QLabel("Год", page))
        bar.addWidget(self.budget_year)
        set_button = QPushButton("Установить бюджет месяца", page)
        set_button.setObjectName("Primary")
        set_button.clicked.connect(self.edit_budget)
        bar.addWidget(set_button)
        bar.addStretch(1)
        self.budget_summary = Hint("", page)
        bar.addWidget(self.budget_summary)
        layout.addLayout(bar)

        layout.addWidget(Hint(
            "В расчёт входят только оплаты поставщикам. Превышение не запрещает "
            "создавать оплаты — оно предупреждает. Двойной щелчок по месяцу "
            "открывает его бюджет.", page))

        self.budget_table = DataTable(self._budget_columns(), page)
        self.budget_table.item_activated.connect(self._edit_budget_row)
        layout.addWidget(self.budget_table, 1)
        return page

    def _budget_columns(self) -> list[Column]:
        return [
            Column("Месяц", lambda u: MONTHS[u.budget.month - 1], width=120,
                   sort_key=lambda u: u.budget.month),
            Column("Бюджет, ₽", lambda u: money(u.budget.amount) if u.budget.amount else "—",
                   width=150, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda u: u.budget.amount),
            Column("Оплачено, ₽", lambda u: money(u.spent), width=150,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda u: u.spent),
            Column("Предстоит, ₽", lambda u: money(u.planned) if u.planned else "", width=140,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda u: u.planned),
            Column("Использовано, ₽", lambda u: money(u.total), width=150,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda u: u.total),
            Column("Остаток, ₽", lambda u: money(u.left) if u.budget.amount else "",
                   width=140, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   color=lambda u: QColor(Palette.DANGER) if u.over else None,
                   sort_key=lambda u: u.left),
            Column("Исполнение", lambda u: f"{u.percent:.0f} %" if u.budget.amount else "",
                   width=110, color=lambda u: _budget_color(u, self.settings.payment_budget_warn),
                   sort_key=lambda u: u.percent),
            Column("Оплат", lambda u: u.count, width=80,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda u: u.count),
            Column("Заметка", lambda u: u.budget.note, width=220),
        ]

    def refresh_budget(self) -> None:
        year = int(self.budget_year.currentData() or date.today().year)
        rows = [self._budget_use(year, month) for month in range(1, 13)]
        self.budget_table.set_items(rows)
        planned = sum(u.budget.amount for u in rows)
        used = sum(u.total for u in rows)
        over = [u for u in rows if u.over]
        parts = [f"бюджет года: {money(planned)} ₽", f"использовано: {money(used)} ₽"]
        if over:
            parts.append(f"превышений: {len(over)}")
        self.budget_summary.setText(" · ".join(parts))
        self.budget_summary.setStyleSheet(
            f"color: {Palette.DANGER if over else Palette.TEXT_MUTED}; font-size: 12px;")

    def _budget_use(self, year: int, month: int) -> BudgetUse:
        spent, planned, count = analytics.month_totals(self.rows, year, month)
        budget = self._budgets.get((year, month)) or Budget(year=year, month=month)
        return BudgetUse(budget=budget, spent=spent, planned=planned, count=count)

    def edit_budget(self) -> None:
        year = int(self.budget_year.currentData() or date.today().year)
        month = self.month if year == self.year else date.today().month
        self._open_budget(year, month)

    def _edit_budget_row(self, use: BudgetUse) -> None:
        if use is not None:
            self._open_budget(use.budget.year, use.budget.month)

    def _open_budget(self, year: int, month: int) -> None:
        current = self._budgets.get((year, month))
        dialog = BudgetDialog(
            year, month, current, analytics.month_history(self.rows, month), self)
        if not dialog.exec():
            return
        if dialog.dropped:
            store.delete_budget(year, month)
            self.notify(f"Бюджет {MONTHS[month - 1].lower()} {year} убран", ToastKind.INFO)
        else:
            budget = dialog.result_budget()
            store.save_budget(budget)
            self.notify(
                f"Бюджет {budget.title.lower()}: {money(budget.amount)} ₽", ToastKind.SUCCESS)
        self._load_budgets()
        self.refresh_budget()
        self.refresh_calendar()
        self.refresh_dashboard()

    def _load_budgets(self) -> None:
        self._budgets = {b.period: b for b in store.budgets()}
        years = sorted({b.year for b in self._budgets.values()} | {date.today().year})
        current = self.budget_year.currentData()
        self.budget_year.blockSignals(True)
        self.budget_year.clear()
        for year in years:
            self.budget_year.addItem(str(year), year)
        index = self.budget_year.findData(current or date.today().year)
        self.budget_year.setCurrentIndex(max(index, 0))
        self.budget_year.blockSignals(False)

    # =========================================================================
    # Данные
    # =========================================================================

    def current_filter(self) -> Filter:
        selection = Filter()
        if value := self.status_filter.currentData():
            selection.statuses = (PaymentStatus(value),)
        days = int(self.period_filter.currentData() or 0)
        today = date.today()
        if days == -1:
            last = calendar.monthrange(today.year, today.month)[1]
            selection.start = date(today.year, today.month, 1)
            selection.end = date(today.year, today.month, last)
        elif days == -2:
            selection.start = today
        elif days > 0:
            selection.start = today - timedelta(days=days)
        if value := float(self.amount_from.value()):
            selection.amount_from = value
        if value := float(self.amount_to.value()):
            selection.amount_to = value
        if name := self.responsible_filter.currentData():
            selection.responsible = name
        if name := self.operation_filter.currentData():
            selection.operation = name
        return selection

    def reload(self) -> None:
        """Перечитывает выборку. Все четыре вкладки обновляются из неё."""
        self.progress.setVisible(True)
        run_task(
            _load_all, self.current_filter(),
            on_result=self._apply_data,
            on_error=lambda message: self._failed(message))

    def _failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.notify(message, ToastKind.ERROR)

    def _apply_data(self, payload: tuple[list[Payment], dict[str, list[str]], int]) -> None:
        rows, known, overdue = payload
        self.progress.setVisible(False)
        self.rows = rows
        self._known = known
        self.stats = analytics.overview(rows)
        self._loaded = True

        self._fill_filter_lists(known)
        self._load_budgets()
        self.table.set_items(rows)
        self._update_table_summary()
        self.refresh_calendar()
        self.refresh_dashboard()
        self.refresh_budget()

        total = store.count_payments()
        self.empty.setVisible(total == 0)
        self.tabs.setVisible(total > 0)
        self.subtitle.setText(
            f"Оплат в базе: {total} · показано {len(rows)} · "
            f"объём выборки {money(self.stats.total)} ₽")
        unlinked = len(store.unlinked_recipients())
        self.link_button.setText(
            f"Привязать получателей ({unlinked})" if unlinked else "Привязать получателей")
        self.link_button.setEnabled(unlinked > 0)
        if overdue:
            self.notify(f"Просроченными стали {overdue} оплат", ToastKind.WARNING)

    def _fill_filter_lists(self, known: dict[str, list[str]]) -> None:
        for box, key, label in (
            (self.responsible_filter, "responsible", "Все ответственные"),
            (self.operation_filter, "operations", "Все операции"),
        ):
            current = box.currentData()
            box.blockSignals(True)
            box.clear()
            box.addItem(label, "")
            for value in known.get(key, []):
                box.addItem(value, value)
            box.setCurrentIndex(max(box.findData(current), 0))
            box.blockSignals(False)

    def restore(self) -> None:
        """Первое открытие страницы: читает базу и напоминает про импорт."""
        if self._loaded:
            return
        self._connect()
        self.reload()
        self._remind_import()

    def _connect(self) -> None:
        """Вход в общую базу, если он настроен и ещё не выполнен.

        Отказ от входа не блокирует раздел: приложение возвращается к своей
        локальной базе. Это честнее, чем пустой экран, — человек хотя бы видит
        свои прежние оплаты и может работать, пока сеть не вернётся.
        """
        if transport.session.active or not self.settings.payment_server:
            return
        if ensure_session(self.settings, self):
            self.notify(f"Общая база: {transport.session.full_name}",
                        ToastKind.SUCCESS)
            return
        self.notify("Вход не выполнен — показаны оплаты из локальной базы",
                    ToastKind.WARNING)

    def _remind_import(self) -> None:
        """Напоминание о свежей выгрузке — раз в неделю, по понедельникам."""
        if not self.settings.payment_import_reminder:
            return
        today = date.today()
        if today.weekday() != 0:
            return
        stamp = today.isoformat()
        if self.settings.payment_import_seen == stamp:
            return
        self.settings.payment_import_seen = stamp
        self.settings.save()
        last = store.last_import()
        when = f" Прошлый импорт: {last['finished_at'][:10]}." if last else ""
        self.notify(f"Понедельник — время загрузить свежую выгрузку оплат.{when}", ToastKind.INFO)

    # =========================================================================
    # Действия
    # =========================================================================

    def run_import(self) -> None:
        if transport.session.active and not transport.session.is_admin:
            # Отказ здесь, а не после выбора файла: разобрать выгрузку и только
            # потом сообщить, что применить её некуда, — впустую потраченное
            # время пользователя.
            self.notify(
                "Импорт из 1С обновляет оплаты всего отдела и доступен "
                "только администратору", ToastKind.WARNING)
            return
        dialog = ImportDialog(recent=self.settings.recent_payment_import, parent=self)
        dialog.imported.connect(self._after_import)
        dialog.exec()

    def _after_import(self, report) -> None:  # type: ignore[no-untyped-def]
        self.settings.remember_payment_import(report.path)
        self.settings.save()
        self.notify(
            f"Импорт завершён: {report.summary}",
            ToastKind.SUCCESS if report.changes else ToastKind.INFO)
        self.reload()

    def link_recipients(self) -> None:
        suppliers = service.supplier_names()
        if not suppliers:
            self.notify(
                "Карточек поставщиков пока нет — заведите их на вкладке «Поставщики»",
                ToastKind.WARNING)
            return
        candidates = service.link_candidates(limit=200)
        if not candidates:
            self.notify("Все получатели уже привязаны", ToastKind.SUCCESS)
            return
        dialog = RecipientLinkDialog(candidates, suppliers, parent=self)
        if dialog.exec() and dialog.linked:
            self.notify(f"Привязано получателей: {dialog.linked}", ToastKind.SUCCESS)
            self.reload()

    def create_payment(
        self,
        *,
        recipient: str = "",
        amount: float = 0.0,
        pay_date: date | None = None,
        supplier_id: int = 0,
        origin: PaymentOrigin = PaymentOrigin.MANUAL,
        origin_ref: str = "",
        comment: str = "",
    ) -> None:
        """Открывает карточку новой оплаты. Ничего не сохраняет до подтверждения."""
        payment = Payment(
            amount=amount,
            pay_date=pay_date,
            recipient=recipient,
            supplier_id=supplier_id,
            responsible=store.current_user(),
            origin=origin,
            origin_ref=origin_ref,
            comment=comment,
        )
        self.open_payment(payment)

    def plan_from_module(
        self,
        *,
        recipient: str = "",
        supplier_id: int = 0,
        amount: float = 0.0,
        terms_days: int = 0,
        comment: str = "",
        origin: str = "manual",
        origin_ref: str = "",
    ) -> None:
        """Заготовка оплаты по итогу заказа или переоценки.

        Дата считается по отсрочке: заданная в карточке поставщика имеет
        приоритет, иначе берётся медиана из истории оплат этому получателю.
        Выходной сдвигается на рабочий день — в истории на субботу и
        воскресенье приходится около трёх процентов оплат.
        """
        rows = store.list_payments(Filter(recipient=recipient)) if recipient else []
        days = float(terms_days) or planning.payment_terms(rows)
        stats = analytics.supplier_history(recipient, rows) if rows else None
        moment = planning.suggest_date(
            days,
            common_day=stats.common_day if stats and stats.day_share >= 40 else 0)
        note = comment
        if days:
            source = "профиль поставщика" if terms_days else "история оплат"
            note = f"{note}\nОтсрочка {days:.0f} дн ({source})"

        # Ни заказ, ни переоценка не считают денег: заказ переносит количества,
        # переоценка меняет цены. Сумму берём из истории как отправную точку и
        # прямо об этом пишем — иначе подставленное число выглядит как
        # посчитанный итог и уйдёт в оплату непроверенным.
        total = amount
        if not total and stats is not None and stats.median_amount:
            total = stats.median_amount
            note = (f"{note}\nСумма — медиана прошлых оплат "
                    f"({stats.count} шт), проверьте по счёту")

        try:
            kind = PaymentOrigin(origin)
        except ValueError:
            kind = PaymentOrigin.MANUAL
        self.tabs.setCurrentIndex(0)
        self.year, self.month = moment.year, moment.month
        self.refresh_calendar()
        self.create_payment(
            recipient=recipient,
            amount=total,
            pay_date=moment,
            supplier_id=supplier_id,
            origin=kind,
            origin_ref=origin_ref,
            comment=note.strip())

    def open_payment(self, payment: Payment) -> None:
        if payment is None:
            return
        known = self._known
        dialog = PaymentDialog(
            payment,
            recipients=known.get("recipients", []),
            responsible=known.get("responsible", []),
            operations=known.get("operations", []),
            parent=self)
        if not dialog.exec():
            return
        result = dialog.result_payment()
        try:
            store.save_payment(result)
        except ValueError as failure:
            self.notify(str(failure), ToastKind.ERROR)
            return
        self.notify(
            f"{result.title}: {money(result.amount)} ₽ на {result.pay_date:%d.%m.%Y}"
            if result.pay_date else f"{result.title}: сохранено",
            ToastKind.SUCCESS)
        self.reload()

    def focus_search(self) -> None:
        self.tabs.setCurrentIndex(1)
        self.search.setFocus()
        self.search.selectAll()

    def _tab_changed(self, _index: int) -> None:
        if not self._loaded:
            self.reload()


def _load_all(
    selection: Filter,
) -> tuple[list[Payment], dict[str, list[str]], int]:
    """Читает выборку, справочные списки и пересчитывает просрочку."""
    overdue = store.refresh_overdue()
    rows = store.list_payments(selection, order="pay_date DESC, id DESC")
    return rows, store.known_values(), overdue


def _weekday(moment: date) -> str:
    names = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
    return names[moment.weekday()]


def _terms_days(payment: Payment) -> int:
    if payment.pay_date is None or payment.request_date is None:
        return -1
    return (payment.pay_date - payment.request_date).days


def _terms_text(payment: Payment) -> str:
    days = _terms_days(payment)
    return f"{days} дн" if days >= 0 else ""


def _files_text(payment: Payment) -> str:
    if payment.files:
        return f"{payment.files} шт"
    return "есть в 1С" if payment.had_files else ""


def _budget_color(use: BudgetUse, threshold: float) -> QColor | None:
    if use.budget.amount <= 0:
        return None
    if use.over:
        return QColor(Palette.DANGER)
    if use.near(threshold):
        return QColor(Palette.WARNING)
    return QColor(Palette.SUCCESS)


def _day_hint(day: date, paid: float, planned: float, count: int, today: date) -> str:
    """Подсказка столбца дня: факт, план и приглашение открыть день."""
    lines = [f"{day:%d.%m.%Y}, {_weekday(day)}"]
    if not count:
        lines.append("оплат нет")
        return "\n".join(lines)
    if paid:
        lines.append(f"оплачено: {money(paid)} ₽")
    if planned:
        lines.append(f"предстоит: {money(planned)} ₽" + (" (план)" if day > today else ""))
    lines.append(f"платежей: {count}")
    lines.append("")
    lines.append("щелчок — открыть день в календаре")
    return "\n".join(lines)
