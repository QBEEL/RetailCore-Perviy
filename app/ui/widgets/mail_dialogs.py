"""Почта в программе: окно письма с файлами и выбор вложения из входящих.

Обе операции ходят в сеть, поэтому идут в фоновой задаче: окно остаётся
живым, а вместо зависания человек видит, что именно сейчас происходит.
"""
from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QCompleter,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import mail
from .. import icons
from ..tasks import run_task
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle
from .inputs import SelectBox

NOT_CONFIGURED = (
    "Почта не подключена. Откройте «Настройки» → «Почта», введите адрес ящика и "
    "пароль для внешнего приложения.")

# Период и число просматриваемых писем: чем глубже, тем дольше читается ящик.
_PERIODS = ((7, "за 7 дней", 30), (14, "за 14 дней", 40), (30, "за 30 дней", 80))


def _signature(box: mail.Mailbox) -> str:
    return f"\n\nС уважением,\n{box.name}" if box.name else ""


class SendMailDialog(QDialog):
    """Письмо с готовыми файлами: адрес, тема, текст, вложения."""

    def __init__(self, box: mail.Mailbox, files: list[str], subject: str = "",
                 body: str = "", to: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._box = box
        self._files = [path for path in files if path]
        self._busy = False
        self.setWindowTitle("Отправить письмом")
        self.setModal(True)
        self.setMinimumWidth(560)
        self._build(subject, body or f"Добрый день!{_signature(box)}", to)

    def _build(self, subject: str, body: str, to: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle("Отправить письмом"))
        root.addWidget(Hint(f"От кого: {self._box.address}"))

        form = QFormLayout()
        form.setSpacing(Metrics.GAP)

        self.to_field = QLineEdit(to)
        self.to_field.setPlaceholderText("Адрес; несколько — через запятую")
        recent = mail.config.settings.mail_recent_to if mail.config.settings else []
        if recent:
            completer = QCompleter(recent, self)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            self.to_field.setCompleter(completer)
        form.addRow("Кому", self.to_field)

        self.subject_field = QLineEdit(subject)
        form.addRow("Тема", self.subject_field)
        root.addLayout(form)

        self.body_field = QPlainTextEdit(body)
        self.body_field.setMinimumHeight(150)
        root.addWidget(self.body_field)

        for path in self._files:
            row = QLabel(f"Вложение: {os.path.basename(path)} · {_size(path)}")
            row.setToolTip(path)
            root.addWidget(row)

        self.copy_box = QCheckBox("Отправить копию себе")
        root.addWidget(self.copy_box)

        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setStyleSheet(f"color: {Palette.DANGER};")
        self.message.hide()
        root.addWidget(self.message)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.send_button = QPushButton("Отправить")
        self.send_button.setObjectName("Primary")
        self.send_button.setIcon(icons.icon("send", "#ffffff"))
        self.send_button.setDefault(True)
        self.send_button.clicked.connect(self._submit)
        buttons.addWidget(self.send_button)
        root.addLayout(buttons)

        (self.to_field if not to else self.subject_field).setFocus()

    def _submit(self) -> None:
        if self._busy:
            return
        try:
            recipients = mail.parse_recipients(self.to_field.text())
            message = mail.build_message(
                self._box, recipients, self.subject_field.text(),
                self.body_field.toPlainText(), self._files, self.copy_box.isChecked())
        except mail.MailError as error:
            self._show_error(str(error))
            return
        self._recipients = recipients
        self._set_busy(True)
        run_task(mail.send_message, self._box, message,
                 on_result=self._sent, on_error=self._failed)

    def _sent(self, _: object) -> None:
        mail.config.remember_recipients(self._recipients)
        self.accept()

    def _failed(self, text: str) -> None:
        self._set_busy(False)
        self._show_error(text)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.send_button.setEnabled(not busy)
        self.send_button.setText("Отправляется…" if busy else "Отправить")
        for widget in (self.to_field, self.subject_field, self.body_field, self.copy_box):
            widget.setEnabled(not busy)
        if busy:
            self.message.hide()

    def _show_error(self, text: str) -> None:
        self.message.setText(text)
        self.message.show()

    def reject(self) -> None:
        # Письмо уже ушло в сеть: закрыть окно значит не узнать, дошло ли оно.
        if not self._busy:
            super().reject()


class InboxDialog(QDialog):
    """Вложения из входящих: выбрать файл, и он скачается."""

    HEADERS = ("Дата", "От кого", "Тема", "Файл", "Размер")

    def __init__(self, box: mail.Mailbox, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._box = box
        self._items: list[mail.Attachment] = []
        self._busy = False
        self.chosen_path = ""
        self.setWindowTitle("Файлы из почты")
        self.setModal(True)
        self.resize(960, 500)
        self._build()
        self.refresh()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle("Файлы из почты"))
        root.addWidget(Hint(
            f"Входящие {self._box.address}: счета PDF, книги Excel и выгрузки. "
            "Письма остаются непрочитанными."))

        top = QHBoxLayout()
        self.period_box = SelectBox()
        for _, label, _ in _PERIODS:
            self.period_box.addItem(label)
        self.period_box.setCurrentIndex(1)
        self.period_box.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.period_box)
        self.status = QLabel("")
        self.status.setObjectName("Hint")
        top.addWidget(self.status, 1)
        self.refresh_button = QPushButton("Обновить")
        self.refresh_button.setIcon(icons.icon("refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.refresh_button)
        root.addLayout(top)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column, width in ((0, 140), (1, 180), (3, 230), (4, 95)):
            self.table.setColumnWidth(column, width)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.itemDoubleClicked.connect(lambda _item: self._choose())
        root.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Закрыть")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.choose_button = QPushButton("Скачать и открыть")
        self.choose_button.setObjectName("Primary")
        self.choose_button.setEnabled(False)
        self.choose_button.clicked.connect(self._choose)
        buttons.addWidget(self.choose_button)
        root.addLayout(buttons)

    # --- список ---------------------------------------------------------------

    def refresh(self, *_args: object) -> None:
        if self._busy:
            return
        days, _, limit = _PERIODS[self.period_box.currentIndex()]
        self._set_busy(True, "Читаю почту…")
        run_task(mail.list_attachments, self._box, days, limit,
                 on_result=self._loaded, on_error=self._failed, on_progress=self._progress)

    def _progress(self, done: int, total: int) -> None:
        self.status.setText(f"Просматриваю письма: {done} из {total}…")

    def _loaded(self, items: list[mail.Attachment]) -> None:
        self._items = items
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            cells = (_when(item.received), item.sender, item.subject, item.filename,
                     _human(item.size))
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                if column == 4:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, cell)
        self._set_busy(False, f"Найдено файлов: {len(items)}" if items
                       else "За этот период подходящих файлов в почте нет")

    def _failed(self, text: str) -> None:
        self._set_busy(False, "")
        QMessageBox.warning(self, "Почта", text)

    # --- выбор ----------------------------------------------------------------

    def _selected(self) -> mail.Attachment | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._items):
            return None
        return self._items[rows[0].row()]

    def _choose(self) -> None:
        item = self._selected()
        if item is None or self._busy:
            return
        self._set_busy(True, f"Скачиваю {item.filename}…")
        run_task(mail.save_attachment, self._box, item, download_folder(),
                 on_result=self._downloaded, on_error=self._failed)

    def _downloaded(self, path: str) -> None:
        self.chosen_path = path
        self.accept()

    def _set_busy(self, busy: bool, text: str) -> None:
        self._busy = busy
        self.status.setText(text)
        self.table.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy)
        self.period_box.setEnabled(not busy)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.choose_button.setEnabled(not self._busy and self._selected() is not None)


def download_folder() -> str:
    """Куда падают файлы из почты: «Загрузки\\RetailCore», рядом с остальным."""
    base = (QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
            or os.path.expanduser("~"))
    return os.path.join(base, "RetailCore")


def pick_from_mail(parent: QWidget | None) -> str:
    """Окно входящих; путь к скачанному файлу или пустая строка."""
    box = mail.config.mailbox()
    if box is None:
        QMessageBox.information(parent, "Почта", NOT_CONFIGURED)
        return ""
    dialog = InboxDialog(box, parent)
    return dialog.chosen_path if dialog.exec() == QDialog.DialogCode.Accepted else ""


def send_files(parent: QWidget | None, files: list[str], subject: str = "",
               body: str = "", to: str = "") -> bool:
    """Окно письма с вложениями. Истина — письмо ушло."""
    box = mail.config.mailbox()
    if box is None:
        QMessageBox.information(parent, "Почта", NOT_CONFIGURED)
        return False
    missing = [path for path in files if not os.path.exists(path)]
    if missing:
        QMessageBox.warning(parent, "Почта", "Файл не найден: " + os.path.basename(missing[0]))
        return False
    dialog = SendMailDialog(box, files, subject, body, to, parent)
    sent = dialog.exec() == QDialog.DialogCode.Accepted
    if sent and (notify := getattr(parent.window() if parent else None, "notify", None)):
        from .toast import ToastKind

        notify("Письмо отправлено", ToastKind.SUCCESS)
    return sent


def _size(path: str) -> str:
    try:
        return _human(os.path.getsize(path))
    except OSError:
        return "?"


def _human(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} МБ".replace(".", ",")
    return f"{max(1, round(size / 1024))} КБ"


def _when(moment: datetime | None) -> str:
    return moment.strftime("%d.%m.%Y %H:%M") if moment else ""
