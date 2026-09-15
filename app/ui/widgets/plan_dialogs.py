"""План оплат из Excel: выдача шаблона и загрузка присланного файла.

Два окна одного сценария. Первое отдаёт менеджеру пустой шаблон со списками из
базы, второе принимает заполненный и показывает, что изменится, до записи.

Отчёт перед применением здесь обязателен, а не «полезен». Загрузка заменяет
план менеджера за месяц целиком: без предварительного списка человек узнавал бы
об исчезнувших строках уже после того, как они исчезли.
"""
from __future__ import annotations

import os
from datetime import date

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QLabel,
    QListWidget,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from ...core.payments import MONTHS, service
from ...core.payments import data as store
from ...core.payments.plan import PlanReport
from ..tasks import run_task
from ..theme import Metrics, Palette
from .calendar_grid import money
from .common import Divider, Hint, SectionTitle
from .file_picker import EXCEL_FILTER, FilePicker
from .inputs import SelectBox

# Сколько строк отчёта показывается списком. Больше человек всё равно не
# прочитает, а счётчики над списком дают полную картину.
PREVIEW_LIMIT = 200

TEMPLATE_HINT = (
    "Шаблон — обычная книга Excel: дата, поставщик, сумма, ставка НДС и "
    "комментарий. Поставщики и менеджеры подставляются выпадающим списком из "
    "базы, но вписать своего тоже можно. Раздайте один и тот же файл всем "
    "менеджерам — загружаться будут любые заполненные. Пустая ставка НДС "
    "читается как 22 %."
)

LOAD_HINT = (
    "Файл разбирается без записи: сначала будет видно, что изменится. План "
    "менеджера за встреченные в файле месяцы заменяется целиком — строки, "
    "которых в файле больше нет, из календаря исчезнут. Уже оплаченное не "
    "затрагивается никогда."
)


def _years(around: int | None = None) -> list[int]:
    """Годы для выбора периода: прошлый, текущий и следующий."""
    current = around or date.today().year
    return [current - 1, current, current + 1]


class PlanTemplateDialog(QDialog):
    """Выдача пустого шаблона плана."""

    saved = Signal(str)

    def __init__(
        self,
        managers: list[str] | None = None,
        *,
        manager: str = "",
        year: int = 0,
        month: int = 0,
        db_path: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.db_path = db_path
        self.setWindowTitle("Шаблон плана оплат")
        self.setMinimumWidth(520)
        moment = date.today()

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 4, Metrics.PAD, Metrics.PAD + 4, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle("Шаблон для менеджера", self))
        root.addWidget(Hint(TEMPLATE_HINT, self))

        form = QFormLayout()
        form.setSpacing(9)
        self.manager = SelectBox(self)
        self.manager.setEditable(True)
        self.manager.addItem("")
        self.manager.addItems(managers or [])
        self.manager.setCurrentText(manager)
        form.addRow("Менеджер", self.manager)

        self.month = SelectBox(self)
        for number, name in enumerate(MONTHS, start=1):
            self.month.addItem(name, number)
        self.month.setCurrentIndex((month or moment.month) - 1)
        form.addRow("Месяц", self.month)

        self.year = SelectBox(self)
        for value in _years(year or moment.year):
            self.year.addItem(str(value), value)
        self.year.setCurrentIndex(1)
        form.addRow("Год", self.year)
        root.addLayout(form)

        # Имя менеджера в шапке — подсказка, а не пароль: его можно оставить
        # пустым и раздать один файл всем, а чей план пришёл — указать при
        # загрузке.
        root.addWidget(Hint(
            "Менеджера можно не выбирать: тогда в шапке будет пусто, и его "
            "впишет либо сам менеджер, либо вы при загрузке.", self))

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.message = QLabel("", self)
        self.message.setWordWrap(True)
        root.addWidget(self.message)

        root.addStretch(1)
        buttons = QDialogButtonBox(self)
        self.save_button = buttons.addButton(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("Сохранить шаблон…")
        self.save_button.setObjectName("Primary")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setText("Закрыть")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _suggested_name(self) -> str:
        month = int(self.month.currentData() or date.today().month)
        year = int(self.year.currentData() or date.today().year)
        who = self.manager.currentText().strip()
        parts = ["План оплат", MONTHS[month - 1], str(year)]
        if who:
            parts.append(who)
        return " ".join(parts) + ".xlsx"

    def _save(self) -> None:
        destination, _ = QFileDialog.getSaveFileName(
            self, "Куда сохранить шаблон", self._suggested_name(), EXCEL_FILTER)
        if not destination:
            return
        self.save_button.setEnabled(False)
        self.progress.setVisible(True)
        self.message.setText("Собираю шаблон…")
        run_task(
            service.plan_template, destination,
            manager=self.manager.currentText().strip(),
            year=int(self.year.currentData() or 0),
            month=int(self.month.currentData() or 0),
            db_path=self.db_path,
            on_result=self._done,
            on_error=self._failed)

    def _done(self, path: str) -> None:
        self.progress.setVisible(False)
        self.saved.emit(path)
        self.accept()

    def _failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.save_button.setEnabled(True)
        self.message.setText(message)
        self.message.setStyleSheet(f"color: {Palette.DANGER};")


class PlanImportDialog(QDialog):
    """Загрузка заполненного плана: разбор в фоне, отчёт, применение."""

    loaded = Signal(object)

    def __init__(
        self,
        managers: list[str] | None = None,
        *,
        db_path: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.db_path = db_path
        self.report: PlanReport | None = None
        self.setWindowTitle("Загрузка плана оплат")
        self.setMinimumWidth(680)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 4, Metrics.PAD, Metrics.PAD + 4, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(Hint(LOAD_HINT, self))

        form = QFormLayout()
        form.setSpacing(9)
        self.manager = SelectBox(self)
        self.manager.setEditable(True)
        self.manager.addItem("")
        self.manager.addItems(managers or [])
        # Не на каждую букву: разбор ходит в базу, а имя набирают по одной.
        # Пересчёт идёт по выбору из списка и по окончании ввода.
        self.manager.activated.connect(self._manager_changed)
        self.manager.lineEdit().editingFinished.connect(self._manager_changed)
        form.addRow("Чей план", self.manager)
        root.addLayout(form)
        root.addWidget(Hint(
            "Пусто — беру менеджера из шапки файла. Выбранное имя сильнее: "
            "шаблон часто копируют друг у друга и шапку не правят.", self))

        self.picker = FilePicker("Файл плана", "не выбран", self, file_filter=EXCEL_FILTER)
        self.picker.file_selected.connect(self._analyze)
        root.addWidget(self.picker)

        self.progress = QProgressBar(self)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        root.addWidget(Divider(self))
        self.summary = QLabel("", self)
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        self.numbers = QGridLayout()
        self.numbers.setHorizontalSpacing(Metrics.GAP)
        root.addLayout(self.numbers)

        self.details = QListWidget(self)
        self.details.setMinimumHeight(180)
        root.addWidget(self.details, 1)

        buttons = QDialogButtonBox(self)
        self.apply_button = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        self.apply_button.setText("Применить")
        self.apply_button.setObjectName("Primary")
        self.apply_button.setEnabled(False)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # --- разбор ---------------------------------------------------------------

    def _manager_changed(self, _index: int = 0) -> None:
        """Смена менеджера меняет и то, чей план заменяется, — разбираем заново."""
        if self.picker.path:
            self._analyze(self.picker.path)

    def _analyze(self, path: str) -> None:
        self.report = None
        self.apply_button.setEnabled(False)
        self.apply_button.setText("Применить")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.summary.setText("Читаю файл…")
        self.summary.setStyleSheet("")
        self.details.clear()
        _clear(self.numbers)
        run_task(
            service.analyze_plan, path,
            manager=self.manager.currentText().strip(),
            db_path=self.db_path,
            on_result=self._ready,
            on_error=self._failed)

    def _ready(self, report: PlanReport) -> None:
        self.report = report
        self.progress.setVisible(False)
        plan = report.plan
        self.summary.setText(
            f"{plan.name} · {plan.manager} · {plan.months_title} · "
            f"сумма плана {money(plan.total)} ₽")
        self.summary.setStyleSheet("")
        _clear(self.numbers)
        tiles = [
            ("Строк", len(plan.rows), Palette.TEXT_MUTED),
            ("Новых", len(report.created), Palette.SUCCESS),
            ("Изменится", len(report.updated), Palette.WARNING),
            ("Исчезнет", len(report.removed), Palette.DANGER),
            ("Оплачено", len(report.paid), Palette.TEXT_FAINT),
            ("Сумма, ₽", money(report.total), Palette.PRIMARY),
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
            self.apply_button.setText("Изменений нет")

    def _fill_details(self, report: PlanReport) -> None:
        """Построчно: что появится, что поправится, что уйдёт и что пропущено."""
        self.details.clear()
        groups = [
            ("Появится", report.created, Palette.SUCCESS),
            ("Изменится", report.updated, Palette.WARNING),
            ("Исчезнет из плана", report.removed, Palette.DANGER),
            ("Уже оплачено — не трогаю", report.paid, Palette.TEXT_FAINT),
        ]
        for title, payments, colour in groups:
            if not payments:
                continue
            self.details.addItem(f"— {title} ({len(payments)}) —")
            head = self.details.item(self.details.count() - 1)
            head.setForeground(Qt.GlobalColor.gray)
            for payment in payments[:PREVIEW_LIMIT]:
                when = f"{payment.pay_date:%d.%m.%Y}" if payment.pay_date else "без даты"
                self.details.addItem(
                    f"   {when}   {money(payment.amount)} ₽   {payment.title}")
            if len(payments) > PREVIEW_LIMIT:
                self.details.addItem(f"   …ещё {len(payments) - PREVIEW_LIMIT}")
        if report.unlinked:
            self.details.addItem(
                f"— Без карточки поставщика: {report.unlinked} —")
        for problem in report.plan.skipped[:PREVIEW_LIMIT]:
            self.details.addItem(f"   пропущено: {problem}")

    def _failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.report = None
        self.apply_button.setEnabled(False)
        self.summary.setText(message)
        self.summary.setStyleSheet(f"color: {Palette.DANGER};")

    # --- применение -----------------------------------------------------------

    def _apply(self) -> None:
        if self.report is None:
            self.reject()
            return
        self.apply_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, self.report.changes)
        self.summary.setText("Записываю…")
        run_task(
            service.apply_plan, self.report,
            db_path=self.db_path,
            on_result=self._done,
            on_error=self._failed,
            on_progress=self._tick)

    def _tick(self, done: int, total: int) -> None:
        self.progress.setRange(0, total or 0)
        self.progress.setValue(done)

    def _done(self, report: PlanReport) -> None:
        self.loaded.emit(report)
        self.accept()


def _clear(grid: QGridLayout) -> None:
    while grid.count():
        if widget := grid.takeAt(0).widget():
            widget.deleteLater()
