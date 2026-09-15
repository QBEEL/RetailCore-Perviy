"""Выгрузка локальной базы в общую: сравнение, отчёт, отправка.

Окно устроено как загрузка плана и импорт выгрузки — сначала показать, потом
записать. Здесь это важнее всего: записывается не свой файл, а общая база
отдела, и отменить отправку нечем.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QListWidget,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from ...core.payments import service
from ...core.payments.sync import SyncReport
from ..tasks import run_task
from ..theme import Metrics, Palette
from .calendar_grid import money
from .common import Divider, Hint, SectionTitle

# Сколько строк показывать списком. Полная выгрузка — тысячи строк; их никто не
# читает, а счётчики над списком дают полную картину.
PREVIEW_LIMIT = 200

HINT = (
    "Сравниваю локальную базу с общей и показываю, что уйдёт на сервер. "
    "Записи, которых нет у вас, но есть на сервере, не трогаются: пока вы "
    "работали у себя, отдел работал в общей базе. Расхождения решаются в "
    "пользу вашей записи."
)


class SyncDialog(QDialog):
    """Отчёт о выгрузке и её применение."""

    uploaded = Signal(object)

    def __init__(self, *, db_path: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db_path = db_path
        self.report: SyncReport | None = None
        self.setWindowTitle("Выгрузка на сервер")
        self.setMinimumWidth(700)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 4, Metrics.PAD, Metrics.PAD + 4, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle("Локальная база → общая", self))
        root.addWidget(Hint(HINT, self))

        self.progress = QProgressBar(self)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        root.addWidget(Divider(self))
        self.summary = QLabel("Сравниваю базы…", self)
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        self.numbers = QGridLayout()
        self.numbers.setHorizontalSpacing(Metrics.GAP)
        root.addLayout(self.numbers)

        self.details = QListWidget(self)
        self.details.setMinimumHeight(200)
        root.addWidget(self.details, 1)

        buttons = QDialogButtonBox(self)
        self.apply_button = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        self.apply_button.setText("Выгрузить")
        self.apply_button.setObjectName("Primary")
        self.apply_button.setEnabled(False)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._analyze()

    # --- сравнение ------------------------------------------------------------

    def _analyze(self) -> None:
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        run_task(
            service.analyze_upload,
            db_path=self.db_path,
            on_result=self._ready,
            on_error=self._failed,
            on_progress=self._tick)

    def _ready(self, report: SyncReport) -> None:
        self.report = report
        self.progress.setVisible(False)
        self.summary.setText(
            f"{report.summary} · сумма выгружаемого {money(report.total)} ₽")
        self.summary.setStyleSheet("")
        _clear(self.numbers)
        tiles = [
            ("Новых", len(report.created), Palette.SUCCESS),
            ("Обновится", len(report.updated), Palette.WARNING),
            ("Совпадает", len(report.same), Palette.TEXT_FAINT),
            ("Бюджетов", len(report.budgets), Palette.PRIMARY),
            ("Только на сервере", report.only_remote, Palette.TEXT_MUTED),
        ]
        for index, (label, value, colour) in enumerate(tiles):
            caption = QLabel(label, self)
            caption.setObjectName("MetricLabel")
            number = QLabel(str(value), self)
            number.setStyleSheet(f"font-size: 17px; font-weight: 600; color: {colour};")
            self.numbers.addWidget(number, 0, index)
            self.numbers.addWidget(caption, 1, index)
        self._fill_details(report)
        self.apply_button.setEnabled(report.changes > 0)
        if not report.changes:
            self.apply_button.setText("Выгружать нечего")

    def _fill_details(self, report: SyncReport) -> None:
        """Сводка по источнику записей и построчный список расхождений."""
        self.details.clear()
        if rows := report.by_origin():
            self.details.addItem("— Что уходит, по источнику —")
            self._grey(self.details.count() - 1)
            for title, total, fresh in rows:
                self.details.addItem(f"   {title}: {total} · новых {fresh}")
        if report.updated:
            self.details.addItem(f"— Расхождения ({len(report.updated)}) —")
            self._grey(self.details.count() - 1)
            for change in report.updated[:PREVIEW_LIMIT]:
                when = (f"{change.local.pay_date:%d.%m.%Y}"
                        if change.local.pay_date else "без даты")
                self.details.addItem(
                    f"   {when}   {money(change.local.amount)} ₽   "
                    f"{change.title} — {change.what}")
            if len(report.updated) > PREVIEW_LIMIT:
                self.details.addItem(f"   …ещё {len(report.updated) - PREVIEW_LIMIT}")
        if report.budgets:
            self.details.addItem(f"— Бюджеты месяцев ({len(report.budgets)}) —")
            self._grey(self.details.count() - 1)
            for budget in report.budgets[:PREVIEW_LIMIT]:
                self.details.addItem(f"   {budget.title}: {money(budget.amount)} ₽")

    def _grey(self, row: int) -> None:
        if item := self.details.item(row):
            item.setForeground(Qt.GlobalColor.gray)

    def _failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.report = None
        self.apply_button.setEnabled(False)
        self.summary.setText(message)
        self.summary.setStyleSheet(f"color: {Palette.DANGER};")

    # --- отправка -------------------------------------------------------------

    def _apply(self) -> None:
        if self.report is None:
            self.reject()
            return
        self.apply_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, self.report.changes)
        self.summary.setText("Отправляю…")
        run_task(
            service.apply_upload, self.report,
            on_result=self._done,
            on_error=self._failed,
            on_progress=self._tick)

    def _tick(self, done: int, total: int) -> None:
        self.progress.setRange(0, total or 0)
        self.progress.setValue(done)

    def _done(self, report: SyncReport) -> None:
        self.uploaded.emit(report)
        self.accept()


def _clear(grid: QGridLayout) -> None:
    while grid.count():
        if widget := grid.takeAt(0).widget():
            widget.deleteLater()
