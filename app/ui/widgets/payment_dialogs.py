"""Диалоги оплат: карточка платежа, импорт выгрузки, бюджет, привязка получателей.

Карточка платежа показывает рядом историю по этому получателю. Без неё сумму и
дату приходится проверять в другом окне, а решение принимается именно здесь.
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
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core.payments import (
    Budget,
    ImportReport,
    MONTHS,
    Payment,
    PaymentOrigin,
    PaymentStatus,
    STATUS_ORDER,
    SupplierStats,
    analytics,
    importer,
    planning,
    service,
    vat,
)

# Подпись пункта, означающего «сумму налога проставил человек».
MANUAL_VAT = "Вручную"
# Общая база, если выполнен вход, иначе своя локальная — см. core/payments/data.
from ...core.payments import data as store
from .. import icons
from ..tasks import run_task
from ..theme import Metrics, Palette
from .calendar_grid import money
from .common import Divider, Hint, SectionTitle
from .file_picker import CSV_FILTER, FilePicker
from .inputs import DecimalInput, SelectBox

STATUS_COLORS: dict[PaymentStatus, str] = {
    PaymentStatus.PAID: Palette.SUCCESS,
    PaymentStatus.PLANNED: Palette.PRIMARY,
    PaymentStatus.OVERDUE: Palette.DANGER,
    PaymentStatus.MOVED: Palette.WARNING,
    PaymentStatus.CANCELLED: Palette.TEXT_FAINT,
}


class DateInput(QLineEdit):
    """Дата в привычном виде «дд.мм.гггг».

    Взято текстовое поле, а не QDateEdit: у последнего колесо мыши меняет дату
    молча, ровно та же беда, из-за которой в приложении появились свои поля
    ввода чисел.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("дд.мм.гггг")
        self.setMaximumWidth(140)

    def value(self) -> date | None:
        return importer.parse_date(self.text())

    def set_value(self, moment: date | None) -> None:
        self.setText(f"{moment:%d.%m.%Y}" if moment else "")


class PaymentDialog(QDialog):
    """Карточка оплаты: поля, вложения и история по получателю."""

    def __init__(
        self,
        payment: Payment,
        *,
        recipients: list[str] | None = None,
        responsible: list[str] | None = None,
        operations: list[str] | None = None,
        db_path: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.payment = payment
        self.db_path = db_path
        self.setWindowTitle("Оплата" if payment.id else "Новая оплата")
        self.setMinimumWidth(760)

        root = QHBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 4, Metrics.PAD, Metrics.PAD + 4, Metrics.PAD)
        root.setSpacing(Metrics.GAP + 4)
        root.addLayout(self._fields(recipients or [], responsible or [], operations or []), 3)
        root.addLayout(self._history(), 2)
        self._fill()
        self._load_history()
        self._apply_rights()

    # --- левая часть: поля ----------------------------------------------------

    def _fields(
        self,
        recipients: list[str],
        responsible: list[str],
        operations: list[str],
    ) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(Metrics.GAP)

        form = QFormLayout()
        form.setSpacing(9)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.recipient = SelectBox(self)
        self.recipient.setEditable(True)
        self.recipient.addItems(recipients)
        self.recipient.setCurrentText("")
        self.recipient.currentTextChanged.connect(lambda _: self._load_history())
        form.addRow("Поставщик", self.recipient)

        self.amount = DecimalInput(self)
        self.amount.setRange(0.0, 1_000_000_000.0)
        self.amount.setDecimals(2)
        self.amount.setGroupSeparatorShown(True)
        self.amount.setSuffix(" ₽")
        form.addRow("Сумма", self.amount)

        vat_row = QHBoxLayout()
        vat_row.setSpacing(8)

        self.vat_rate = SelectBox(self)
        for rate in vat.RATES:
            self.vat_rate.addItem(rate.title, rate.percent)
        # «Вручную» — не ставка, а признак того, что сумму налога проставил
        # человек. Без него любая правка поля НДС тут же затиралась бы
        # пересчётом по выбранной ставке.
        self.vat_rate.addItem(MANUAL_VAT, -1)
        self.vat_rate.setFixedWidth(110)
        vat_row.addWidget(self.vat_rate)

        self.vat = DecimalInput(self)
        self.vat.setRange(0.0, 1_000_000_000.0)
        self.vat.setDecimals(2)
        self.vat.setGroupSeparatorShown(True)
        self.vat.setSuffix(" ₽")
        vat_row.addWidget(self.vat, 1)
        form.addRow("НДС", vat_row)

        self.net_hint = QLabel("", self)
        self.net_hint.setObjectName("Hint")
        form.addRow("", self.net_hint)

        self.amount.valueChanged.connect(self._recalc_vat)
        self.vat_rate.currentIndexChanged.connect(self._recalc_vat)
        self.vat.valueChanged.connect(self._vat_edited)

        date_row = QHBoxLayout()
        date_row.setSpacing(8)
        self.pay_date = DateInput(self)
        date_row.addWidget(self.pay_date)
        self.terms_hint = QLabel("", self)
        self.terms_hint.setObjectName("Hint")
        date_row.addWidget(self.terms_hint, 1)
        form.addRow("Дата оплаты", date_row)

        self.status = SelectBox(self)
        for status in STATUS_ORDER:
            self.status.addItem(status.title, status.value)
        form.addRow("Статус", self.status)

        self.responsible = SelectBox(self)
        self.responsible.setEditable(True)
        self.responsible.addItems(responsible)
        form.addRow("Ответственный", self.responsible)

        self.operation = SelectBox(self)
        self.operation.setEditable(True)
        self.operation.addItems(operations or ["Оплата поставщику"])
        form.addRow("Операция", self.operation)

        column.addLayout(form)

        column.addWidget(QLabel("Комментарий", self))
        self.comment = QPlainTextEdit(self)
        self.comment.setMaximumHeight(66)
        self.comment.setStyleSheet(
            f"background: {Palette.SURFACE}; border: 1px solid {Palette.BORDER_STRONG};"
            f" border-radius: {Metrics.RADIUS_SM}px; padding: 6px;")
        column.addWidget(self.comment)

        column.addWidget(Divider(self))
        files_row = QHBoxLayout()
        files_row.addWidget(SectionTitle("Документы", self))
        files_row.addStretch(1)
        self.attach_button = QPushButton("Приложить", self)
        self.attach_button.setObjectName("Ghost")
        self.attach_button.setIcon(icons.icon("open"))
        self.attach_button.clicked.connect(self._attach)
        files_row.addWidget(self.attach_button)
        self.detach_button = QPushButton("Убрать", self)
        self.detach_button.setObjectName("Ghost")
        self.detach_button.setIcon(icons.icon("trash"))
        self.detach_button.clicked.connect(self._detach)
        files_row.addWidget(self.detach_button)
        column.addLayout(files_row)

        self.files = QListWidget(self)
        self.files.setMaximumHeight(84)
        self.files.itemDoubleClicked.connect(self._open_file)
        column.addWidget(self.files)
        self.files_hint = Hint("", self)
        column.addWidget(self.files_hint)

        column.addStretch(1)
        self.error = Hint("", self)
        self.error.setStyleSheet(f"color: {Palette.DANGER}; font-size: 12px;")
        column.addWidget(self.error)

        buttons = QDialogButtonBox(self)
        self.save_button = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        self.save_button.setText("Сохранить")
        self.save_button.setObjectName("Primary")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        column.addWidget(buttons)
        return column

    # --- НДС ------------------------------------------------------------------

    def _chosen_rate(self) -> "vat.Rate | None":
        percent = self.vat_rate.currentData()
        return None if percent is None or percent < 0 else vat.of(percent)

    def _recalc_vat(self) -> None:
        """Пересчитывает налог по выбранной ставке. «Вручную» не трогает."""
        rate = self._chosen_rate()
        if rate is None:
            self._show_net()
            return
        amount = float(self.amount.value())
        # Сигнал глушится, иначе запись значения выглядит как правка руками и
        # тут же переключает ставку на «Вручную».
        self.vat.blockSignals(True)
        self.vat.setValue(rate.vat_of(amount))
        self.vat.blockSignals(False)
        self._show_net()

    def _vat_edited(self) -> None:
        """Человек исправил налог сам — ставка уступает ему место."""
        if self._chosen_rate() is not None:
            self.vat_rate.blockSignals(True)
            self.vat_rate.setCurrentIndex(self.vat_rate.count() - 1)
            self.vat_rate.blockSignals(False)
        self._show_net()

    def _show_net(self) -> None:
        """Подпись «сумма без НДС» — чтобы цифру можно было сверить глазами."""
        amount = float(self.amount.value())
        tax = float(self.vat.value())
        if amount <= 0:
            self.net_hint.setText("")
            return
        self.net_hint.setText(f"сумма без НДС: {money(amount - tax)} ₽")

    def _adopt_vat(self) -> None:
        """Ставка существующей оплаты — по её сумме и налогу.

        Не опознали — значит, налог проставлен руками или запись старая.
        Подставлять ближайшую ставку нельзя: она молча переписала бы сумму
        налога при первом же сохранении.
        """
        rate = vat.detect(float(self.amount.value()), float(self.vat.value()))
        index = (self.vat_rate.findData(rate.percent) if rate
                 else self.vat_rate.count() - 1)
        self.vat_rate.blockSignals(True)
        self.vat_rate.setCurrentIndex(index)
        self.vat_rate.blockSignals(False)
        self._show_net()

    def _suggest_rate(self) -> None:
        """Ставка нового платежа — по прошлым оплатам этого получателя.

        У поставщика ставка меняется редко, и своя полезнее общей: упрощенец
        с пятипроцентной иначе требовал бы правки при каждом создании.
        """
        if self.payment.id or not self._history:
            return
        rate = vat.guess_by_history(
            [(float(p.amount), float(p.vat)) for p in self._history])
        if rate is None:
            return
        self.vat_rate.setCurrentIndex(self.vat_rate.findData(rate.percent))

    def _apply_rights(self) -> None:
        """Чужая оплата открывается только на просмотр.

        Запрет виден сразу, а не всплывает отказом сервера при сохранении:
        человек не должен заполнять карточку, чтобы узнать, что она не его.
        """
        if store.may_edit(self.payment):
            return
        for widget in (self.recipient, self.amount, self.vat, self.vat_rate,
                       self.pay_date, self.status, self.responsible,
                       self.operation, self.comment,
                       self.attach_button, self.detach_button):
            widget.setEnabled(False)
        self.save_button.setEnabled(False)
        self.save_button.setText("Только просмотр")
        owner = self.payment.responsible or "другого менеджера"
        self.error.setText(f"Оплата закреплена за: {owner}. "
                           "Изменить её может только этот менеджер.")

    # --- правая часть: история ------------------------------------------------

    def _history(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(6)
        column.addWidget(SectionTitle("История по поставщику", self))
        self.history_summary = Hint("выберите поставщика", self)
        column.addWidget(self.history_summary)
        column.addWidget(Divider(self))
        self.history = QListWidget(self)
        self.history.setStyleSheet("font-size: 12px;")
        column.addWidget(self.history, 1)
        self.suggest_button = QPushButton("Подставить по истории", self)
        self.suggest_button.setIcon(icons.icon("star"))
        self.suggest_button.setEnabled(False)
        self.suggest_button.clicked.connect(self._apply_suggestion)
        column.addWidget(self.suggest_button)
        return column

    def _load_history(self) -> None:
        name = self.recipient.currentText().strip()
        self.stats: SupplierStats | None = None
        self.terms_days = 0.0
        self._history: list[Payment] = []
        self.history.clear()
        if not name:
            self.history_summary.setText("выберите поставщика")
            self.suggest_button.setEnabled(False)
            self.terms_hint.setText("")
            return
        run_task(
            _supplier_history, name, self.db_path,
            on_result=self._show_history,
            on_error=lambda message: self.history_summary.setText(message))

    def _show_history(self, payload: tuple[SupplierStats, float, list[Payment]]) -> None:
        stats, terms, recent = payload
        self.stats = stats
        self.terms_days = terms
        self._history = list(recent)
        self._suggest_rate()
        if not stats.count:
            self.history_summary.setText("оплат этому поставщику ещё не было")
            self.suggest_button.setEnabled(False)
            self.terms_hint.setText("")
            return
        parts = [
            f"оплат: {stats.count}",
            f"всего {money(stats.total)} ₽",
            f"средняя {money(stats.average)} ₽",
        ]
        if stats.median_interval:
            parts.append(f"обычно каждые {stats.median_interval:.0f} дн")
        if stats.common_day and stats.day_share >= 30:
            parts.append(f"чаще {stats.common_day}-го числа")
        if stats.last_pay:
            parts.append(f"последняя {stats.last_pay:%d.%m.%Y}")
        self.history_summary.setText(" · ".join(parts))
        self.terms_hint.setText(
            f"отсрочка по истории: {terms:.0f} дн" if terms else "отсрочки в истории нет")
        for payment in recent:
            when = f"{payment.pay_date:%d.%m.%Y}" if payment.pay_date else "без даты  "
            item = QListWidgetItem(
                f"{when}   {money(payment.amount)} ₽   {payment.status.title}")
            item.setForeground(Qt.GlobalColor.darkGray)
            self.history.addItem(item)
        self.suggest_button.setEnabled(bool(terms or stats.median_amount))

    def _apply_suggestion(self) -> None:
        """Подставляет дату по отсрочке и сумму по медиане — без сохранения."""
        if self.stats is None:
            return
        moment = planning.suggest_date(
            self.terms_days or self.stats.median_interval,
            common_day=self.stats.common_day if self.stats.day_share >= 40 else 0)
        self.pay_date.set_value(moment)
        if self.amount.value() <= 0 and self.stats.median_amount:
            self.amount.setValue(self.stats.median_amount)

    # --- значения -------------------------------------------------------------

    def _fill(self) -> None:
        payment = self.payment
        if payment.recipient:
            self.recipient.setCurrentText(payment.recipient)
        # Сигналы глушатся на время заполнения: иначе установка суммы успевает
        # пересчитать налог по ставке из списка и на мгновение затирает
        # сохранённое значение. Итог был бы тот же, но держаться на порядке
        # двух соседних строк такая вещь не должна.
        self.amount.blockSignals(True)
        self.vat.blockSignals(True)
        self.amount.setValue(payment.amount)
        self.vat.setValue(payment.vat)
        self.amount.blockSignals(False)
        self.vat.blockSignals(False)
        # Ставка выводится из уже записанных суммы и налога, а не наоборот:
        # сохранённые цифры менять при простом открытии карточки нельзя.
        self._adopt_vat()
        self.pay_date.set_value(payment.pay_date)
        self.status.setCurrentIndex(max(self.status.findData(payment.status.value), 0))
        self.responsible.setCurrentText(payment.responsible or store.current_user())
        self.operation.setCurrentText(payment.operation or "Оплата поставщику")
        self.comment.setPlainText(payment.comment)
        self._reload_files()
        if payment.doc_number:
            self.files_hint.setText(
                f"из 1С: заявка {payment.doc_number}"
                + (f" от {payment.request_date:%d.%m.%Y}" if payment.request_date else "")
                + (f" · автор {payment.author}" if payment.author else "")
                + (" · во вложении 1С есть файлы" if payment.had_files else ""))

    def _reload_files(self) -> None:
        self.files.clear()
        if not self.payment.id:
            self.detach_button.setEnabled(False)
            return
        attachments = store.files(self.payment.id, self.db_path)
        for attachment in attachments:
            item = QListWidgetItem(f"{attachment.name}   ({attachment.size // 1024} КБ)")
            item.setData(Qt.ItemDataRole.UserRole, attachment.id)
            if not store.file_available(attachment):
                item.setText(f"{attachment.name}   (файл не найден)")
                item.setForeground(Qt.GlobalColor.red)
            self.files.addItem(item)
        self.detach_button.setEnabled(bool(attachments))

    def _attach(self) -> None:
        if not self.payment.id:
            self.error.setText("Сначала сохраните оплату — потом приложите документы")
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Документы к оплате", "", "Все файлы (*.*)")
        for path in paths:
            try:
                store.attach_file(self.payment.id, path, self.db_path)
            except (OSError, ValueError) as failure:
                self.error.setText(str(failure))
        self._reload_files()

    def _detach(self) -> None:
        item = self.files.currentItem()
        if item is None:
            return
        store.detach_file(int(item.data(Qt.ItemDataRole.UserRole)), self.db_path)
        self._reload_files()

    def _open_file(self, item: QListWidgetItem) -> None:
        wanted = int(item.data(Qt.ItemDataRole.UserRole))
        for attachment in store.files(self.payment.id, self.db_path):
            if attachment.id != wanted or not store.file_available(attachment):
                continue
            try:
                # У серверного вложения локального пути нет — оно скачивается
                # при первом открытии и потом берётся из кэша.
                os.startfile(store.open_path(attachment))  # noqa: S606
            except (OSError, ValueError) as failure:
                self.error.setText(f"Не удалось открыть вложение: {failure}")
            return

    def result_payment(self) -> Payment:
        payment = self.payment
        payment.recipient = self.recipient.currentText().strip()
        payment.amount = float(self.amount.value())
        payment.vat = float(self.vat.value())
        payment.pay_date = self.pay_date.value()
        payment.status = PaymentStatus(self.status.currentData())
        payment.paid_flag = payment.status is PaymentStatus.PAID
        payment.responsible = self.responsible.currentText().strip()
        payment.operation = self.operation.currentText().strip()
        payment.comment = self.comment.toPlainText().strip()
        if not payment.id and payment.origin is PaymentOrigin.IMPORT:
            payment.origin = PaymentOrigin.MANUAL
        return payment

    def _accept(self) -> None:
        payment = self.result_payment()
        if payment.amount <= 0:
            self.error.setText("Укажите сумму больше нуля")
            return
        if not payment.recipient:
            self.error.setText("Укажите поставщика")
            return
        if payment.status is not PaymentStatus.CANCELLED and payment.pay_date is None:
            self.error.setText("Укажите дату оплаты — без неё платёж не попадёт в календарь")
            return
        self.accept()


def _supplier_history(
    recipient: str,
    db_path: str | None,
) -> tuple[SupplierStats, float, list[Payment]]:
    """История получателя для карточки: показатели, отсрочка, последние оплаты."""
    rows = store.list_payments(
        store.Filter(recipient=recipient), db_path, order="pay_date DESC", limit=400)
    stats = analytics.supplier_history(recipient, rows)
    return stats, planning.payment_terms(rows), rows[:14]


class ImportDialog(QDialog):
    """Импорт выгрузки: разбор в фоне, предпросмотр, применение."""

    imported = Signal(object)

    def __init__(
        self,
        *,
        recent: list[str] | None = None,
        db_path: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.db_path = db_path
        self.report: ImportReport | None = None
        self.setWindowTitle("Импорт оплат из 1С")
        self.setMinimumWidth(660)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 4, Metrics.PAD, Metrics.PAD + 4, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Hint(
            "Выгрузка «Оплата поставщикам» из 1С в формате CSV. Файл разбирается "
            "без записи — сначала будет видно, что именно изменится. Повторный "
            "импорт того же файла ничего не создаёт: записи опознаются по паре "
            "«номер заявки + дата заявки».", self))

        self.picker = FilePicker("Файл выгрузки", "не выбран", self, file_filter=CSV_FILTER)
        self.picker.set_recent(recent or [])
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

        self.skipped = QListWidget(self)
        self.skipped.setMaximumHeight(96)
        self.skipped.setVisible(False)
        root.addWidget(self.skipped)

        root.addStretch(1)
        buttons = QDialogButtonBox(self)
        self.apply_button = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        self.apply_button.setText("Импортировать")
        self.apply_button.setObjectName("Primary")
        self.apply_button.setEnabled(False)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _analyze(self, path: str) -> None:
        self.report = None
        self.apply_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.summary.setText("Читаю файл…")
        self.skipped.setVisible(False)
        _clear(self.numbers)
        if previous := service.already_imported(path, self.db_path):
            self.summary.setText(
                f"Этот файл уже импортировали {previous:%d.%m.%Y в %H:%M}. "
                "Повторный импорт ничего не сломает — новых записей просто не будет.")
        run_task(
            service.analyze_import, path,
            db_path=self.db_path,
            on_result=self._ready,
            on_error=self._failed,
            on_progress=self._tick)

    def _tick(self, done: int, total: int) -> None:
        self.progress.setRange(0, total or 0)
        self.progress.setValue(done)

    def _ready(self, report: ImportReport) -> None:
        self.report = report
        self.progress.setVisible(False)
        period = ""
        if report.first_pay and report.last_pay:
            period = f" · платежи с {report.first_pay:%d.%m.%Y} по {report.last_pay:%d.%m.%Y}"
        self.summary.setText(
            f"{os.path.basename(report.path)}{period} · получателей: {report.recipients}")
        _clear(self.numbers)
        tiles = [
            ("Прочитано", report.rows, Palette.TEXT_MUTED),
            ("Новых", report.new, Palette.SUCCESS),
            ("Изменится", report.updated, Palette.WARNING),
            ("Без изменений", report.same, Palette.TEXT_FAINT),
            ("Сумма, ₽", money(report.total), Palette.PRIMARY),
        ]
        for index, (label, value, colour) in enumerate(tiles):
            caption = QLabel(label, self)
            caption.setObjectName("MetricLabel")
            number = QLabel(str(value), self)
            number.setStyleSheet(f"font-size: 17px; font-weight: 600; color: {colour};")
            self.numbers.addWidget(number, 0, index)
            self.numbers.addWidget(caption, 1, index)
        if report.skipped:
            self.skipped.clear()
            self.skipped.addItems(report.skipped[:200])
            self.skipped.setVisible(True)
        self.apply_button.setEnabled(report.changes > 0)
        if not report.changes:
            self.apply_button.setText("Изменений нет")

    def _failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.report = None
        self.apply_button.setEnabled(False)
        self.summary.setText(message)
        self.summary.setStyleSheet(f"color: {Palette.DANGER};")

    def _apply(self) -> None:
        if self.report is None:
            self.reject()
            return
        self.apply_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.summary.setText("Записываю…")
        run_task(
            service.apply_import, self.report,
            db_path=self.db_path,
            on_result=self._done,
            on_error=self._failed)

    def _done(self, report: ImportReport) -> None:
        self.imported.emit(report)
        self.accept()


class BudgetDialog(QDialog):
    """Бюджет месяца с подсказкой по прошлым годам."""

    def __init__(
        self,
        year: int,
        month: int,
        current: Budget | None = None,
        history: list[float] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.year, self.month = year, month
        self.setWindowTitle(f"Бюджет · {MONTHS[month - 1]} {year}")
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 4, Metrics.PAD, Metrics.PAD + 4, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        form = QFormLayout()
        form.setSpacing(9)
        self.amount = DecimalInput(self)
        self.amount.setRange(0.0, 100_000_000_000.0)
        self.amount.setDecimals(2)
        self.amount.setGroupSeparatorShown(True)
        self.amount.setSuffix(" ₽")
        self.amount.setValue(current.amount if current else 0.0)
        form.addRow("Бюджет месяца", self.amount)
        self.note = QLineEdit(current.note if current else "", self)
        self.note.setPlaceholderText("необязательно")
        form.addRow("Заметка", self.note)
        root.addLayout(form)

        past = [value for value in (history or []) if value > 0]
        if past:
            average = sum(past) / len(past)
            root.addWidget(Hint(
                f"В этом месяце прошлых лет уходило: "
                + " · ".join(f"{money(value)} ₽" for value in past[-4:])
                + f"\nСреднее: {money(average)} ₽", self))
            use = QPushButton(f"Взять среднее — {money(average)} ₽", self)
            use.setObjectName("Ghost")
            use.clicked.connect(lambda: self.amount.setValue(average))
            root.addWidget(use)
        else:
            root.addWidget(Hint("Истории по этому месяцу пока нет.", self))

        root.addWidget(Hint(
            "В бюджет попадают только оплаты поставщикам. Налоги, аренда и "
            "зарплата в расчёт месяца не входят.", self))

        root.addStretch(1)
        buttons = QDialogButtonBox(self)
        save = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        save.setText("Сохранить")
        save.setObjectName("Primary")
        if current is not None:
            remove = buttons.addButton("Убрать бюджет", QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.setObjectName("Danger")
            remove.clicked.connect(self._drop)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.dropped = False

    def _drop(self) -> None:
        self.dropped = True
        self.accept()

    def result_budget(self) -> Budget:
        return Budget(
            year=self.year,
            month=self.month,
            amount=float(self.amount.value()),
            note=self.note.text().strip(),
        )


class BulkEditDialog(QDialog):
    """Массовое изменение выделенных оплат. Пустое поле не меняется."""

    def __init__(
        self,
        count: int,
        responsible: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Изменить {count} оплат")
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 4, Metrics.PAD, Metrics.PAD + 4, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(Hint(
            f"Изменения применятся ко всем {count} выделенным оплатам. "
            "Незаполненное поле остаётся как было.", self))

        form = QFormLayout()
        form.setSpacing(9)
        self.status = SelectBox(self)
        self.status.addItem("— не менять —", "")
        for status in STATUS_ORDER:
            self.status.addItem(status.title, status.value)
        form.addRow("Статус", self.status)

        self.pay_date = DateInput(self)
        form.addRow("Дата оплаты", self.pay_date)

        self.responsible = SelectBox(self)
        self.responsible.setEditable(True)
        self.responsible.addItem("")
        self.responsible.addItems(responsible or [])
        self.responsible.setCurrentText("")
        form.addRow("Ответственный", self.responsible)
        root.addLayout(form)

        root.addStretch(1)
        buttons = QDialogButtonBox(self)
        apply_button = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        apply_button.setText("Применить")
        apply_button.setObjectName("Primary")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def changes(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if value := self.status.currentData():
            result["status"] = PaymentStatus(value)
        if moment := self.pay_date.value():
            result["pay_date"] = moment
        if name := self.responsible.currentText().strip():
            result["responsible"] = name
        return result


class RecipientLinkDialog(QDialog):
    """Связывание получателей 1С с карточками поставщиков."""

    def __init__(
        self,
        candidates: list[tuple[str, int, float, object]],
        suppliers: dict[int, str],
        *,
        db_path: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.db_path = db_path
        self.suppliers = suppliers
        self.linked = 0
        self.setWindowTitle("Получатели без карточки поставщика")
        self.setMinimumSize(760, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 4, Metrics.PAD, Metrics.PAD + 4, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(Hint(
            "Получатель в 1С записан юрлицом, а карточка поставщика — торговым "
            "именем. Привязка нужна для отсрочки и связи с заказами; без неё "
            "оплата всё равно считается по имени. Уверенные совпадения "
            "подставлены, остальные оставлены пустыми намеренно — неверная "
            "привязка хуже её отсутствия.", self))

        self.rows: list[tuple[str, SelectBox]] = []
        holder = QWidget(self)
        grid = QGridLayout(holder)
        grid.setHorizontalSpacing(Metrics.GAP)
        grid.setVerticalSpacing(5)
        grid.addWidget(_bold("Получатель из 1С", self), 0, 0)
        grid.addWidget(_bold("Оплат", self), 0, 1)
        grid.addWidget(_bold("Сумма", self), 0, 2)
        grid.addWidget(_bold("Карточка поставщика", self), 0, 3)
        for row, (recipient, count, total, guess) in enumerate(candidates, start=1):
            grid.addWidget(QLabel(recipient[:44], self), row, 0)
            grid.addWidget(QLabel(str(count), self), row, 1)
            grid.addWidget(QLabel(money(total), self), row, 2)
            box = SelectBox(self)
            box.setMinimumWidth(230)
            box.addItem("— не привязывать —", 0)
            for supplier_id, name in sorted(suppliers.items(), key=lambda item: item[1]):
                box.addItem(name, supplier_id)
            if guess is not None and getattr(guess, "confident", False):
                box.setCurrentIndex(max(box.findData(guess.supplier_id), 0))
                box.setToolTip(getattr(guess, "reason", ""))
            grid.addWidget(box, row, 3)
            self.rows.append((recipient, box))
        grid.setColumnStretch(0, 1)

        area = QScrollArea(self)
        area.setWidget(holder)
        area.setWidgetResizable(True)
        root.addWidget(area, 1)

        buttons = QDialogButtonBox(self)
        save = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        save.setText("Привязать")
        save.setObjectName("Primary")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _apply(self) -> None:
        for recipient, box in self.rows:
            if supplier_id := int(box.currentData() or 0):
                store.save_recipient_link(recipient, supplier_id, self.db_path)
                self.linked += 1
        self.accept()


def _bold(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    label.setStyleSheet(f"font-weight: 600; font-size: 12px; color: {Palette.TEXT_MUTED};")
    return label


def _clear(grid: QGridLayout) -> None:
    while grid.count():
        if widget := grid.takeAt(0).widget():
            widget.deleteLater()
