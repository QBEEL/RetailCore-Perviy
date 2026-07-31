"""Смена пароля.

Открывается сразу после входа, если пароль выдан администратором: такой пароль
видел не только его владелец, и пока он не заменён, записи в журнале нельзя
считать доказательством того, кто именно правил оплату.

Отменить нельзя — можно только выйти. Иначе требование превратилось бы в
предложение, которое все закрывают крестиком.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ...core.payments import transport
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle

# Короткий пароль подбирается быстрее, чем успевает сработать защита от
# перебора: она придерживает пять попыток за четверть часа, а не за всё время.
MIN_LENGTH = 10


class _Change(QThread):
    done = Signal()
    failed = Signal(str)

    def __init__(self, old: str, new: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._old, self._new = old, new

    def run(self) -> None:
        try:
            transport.change_password(self._old, self._new)
        except transport.ServerError as error:
            self.failed.emit(str(error))
        except Exception as error:  # noqa: BLE001 — окно не должно падать молча
            self.failed.emit(f"Непредвиденная ошибка: {error}")
        else:
            self.done.emit()


class PasswordDialog(QDialog):
    """Текущий пароль и новый дважды."""

    def __init__(self, *, required: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.required = required
        self._worker: _Change | None = None
        self.setWindowTitle("Смена пароля")
        self.setModal(True)
        self.setMinimumWidth(420)
        if required:
            # Крестик в заголовке убран: закрыть окно, не сменив пароль, нельзя.
            self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(SectionTitle("Смена пароля"))
        if self.required:
            root.addWidget(Hint(
                "Пароль выдан администратором и известен не только вам. "
                "Придумайте свой — под ним будут записаны ваши правки."))

        form = QFormLayout()
        form.setSpacing(Metrics.GAP)

        self.old = QLineEdit()
        self.old.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Текущий пароль", self.old)

        self.new = QLineEdit()
        self.new.setEchoMode(QLineEdit.EchoMode.Password)
        self.new.setPlaceholderText(f"не короче {MIN_LENGTH} символов")
        form.addRow("Новый пароль", self.new)

        self.repeat = QLineEdit()
        self.repeat.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Ещё раз", self.repeat)

        root.addLayout(form)

        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setStyleSheet(f"color: {Palette.DANGER};")
        self.message.hide()
        root.addWidget(self.message)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        self.buttons.button(
            QDialogButtonBox.StandardButton.Ok).setText("Сменить пароль")
        self.buttons.accepted.connect(self._submit)
        if not self.required:
            cancel = self.buttons.addButton(
                QDialogButtonBox.StandardButton.Cancel)
            cancel.setText("Отмена")
            self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        for field in (self.old, self.new, self.repeat):
            field.returnPressed.connect(self._submit)
        self.old.setFocus()

    def _submit(self) -> None:
        if self.new.text() != self.repeat.text():
            self._show_error("Новый пароль введён по-разному")
            return
        if len(self.new.text()) < MIN_LENGTH:
            self._show_error(f"Пароль должен быть не короче {MIN_LENGTH} символов")
            return
        if self.new.text() == self.old.text():
            self._show_error("Новый пароль должен отличаться от прежнего")
            return

        self._busy(True)
        self._worker = _Change(self.old.text(), self.new.text(), self)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _busy(self, busy: bool) -> None:
        self.buttons.setEnabled(not busy)
        for field in (self.old, self.new, self.repeat):
            field.setEnabled(not busy)

    def _show_error(self, text: str) -> None:
        self.message.setText(text)
        self.message.show()

    def _on_done(self) -> None:
        self._busy(False)
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._busy(False)
        self._show_error(message)

    def reject(self) -> None:
        # Требование не обходится клавишей Escape.
        if not self.required:
            super().reject()
