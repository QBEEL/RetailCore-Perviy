"""Замещение на время отпуска: кто, кого и на какой срок."""
from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ...core.payments.admin import Account, Cover
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle
from .payment_dialogs import DateInput


class CoverDialog(QDialog):
    """Назначение или правка замещения.

    Людей предлагают из действующих учёток: заместить может только тот, кто
    способен войти. Отсутствующим же может оказаться любой, в том числе уже
    отключённый, поэтому в первом списке остаются все.
    """

    def __init__(self, accounts: list[Account], cover: Cover | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cover = cover or Cover()
        self.setWindowTitle("Замещение" if self.cover.id else "Новое замещение")
        self.setMinimumWidth(520)
        self._build(sorted(accounts, key=lambda a: a.title.lower()))

    def _build(self, accounts: list[Account]) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(SectionTitle("Кто правит оплаты на время отсутствия"))
        root.addWidget(Hint(
            "Пока идёт срок, заместитель видит оплаты отсутствующего своими и "
            "может их править, прикладывать и убирать вложения. По окончании "
            "срока право пропадает само. За отсутствующим оплаты остаются "
            "записанными — в таблице видно, чьи они на самом деле."))

        form = QFormLayout()
        form.setSpacing(Metrics.GAP)

        self.absent = QComboBox(self)
        for account in accounts:
            self.absent.addItem(account.title, account.id)
        form.addRow("Уходит в отпуск", self.absent)

        self.cover_box = QComboBox(self)
        for account in accounts:
            if account.is_active:
                self.cover_box.addItem(account.title, account.id)
        form.addRow("Замещает", self.cover_box)

        today = date.today()
        self.date_from = DateInput(self)
        self.date_from.set_value(self.cover.date_from or today)
        form.addRow("С", self.date_from)
        self.date_to = DateInput(self)
        self.date_to.set_value(self.cover.date_to or today + timedelta(days=13))
        form.addRow("По (включительно)", self.date_to)

        self.note = QLineEdit(self.cover.note, self)
        self.note.setMaxLength(300)
        self.note.setPlaceholderText("Например: отпуск")
        form.addRow("Примечание", self.note)
        root.addLayout(form)

        if self.cover.id:
            self._select(self.absent, self.cover.absent_id)
            self._select(self.cover_box, self.cover.cover_id)

        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {Palette.DANGER};")
        self.error.hide()
        root.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("Primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _select(box: QComboBox, value: int) -> None:
        index = box.findData(value)
        if index >= 0:
            box.setCurrentIndex(index)

    def _accept(self) -> None:
        start, end = self.date_from.value(), self.date_to.value()
        if self.absent.currentData() is None or self.cover_box.currentData() is None:
            self._fail("Выберите, кто уходит и кто замещает")
        elif self.absent.currentData() == self.cover_box.currentData():
            self._fail("Человек не может замещать сам себя")
        elif start is None or end is None:
            self._fail("Укажите даты в виде дд.мм.гггг")
        elif end < start:
            self._fail("Конец замещения раньше начала")
        else:
            self.accept()

    def _fail(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()

    def result_cover(self) -> Cover:
        self.cover.absent_id = int(self.absent.currentData())
        self.cover.cover_id = int(self.cover_box.currentData())
        self.cover.date_from = self.date_from.value()
        self.cover.date_to = self.date_to.value()
        self.cover.note = self.note.text().strip()
        return self.cover


def state_color(cover: Cover) -> QColor | None:
    """Закончившиеся замещения бледнеют — идущие и будущие остаются заметными."""
    return QColor(Palette.TEXT_FAINT) if cover.state == "ended" else None
