"""Подтверждение удаления данных общей базы.

Отдельное окно, а не обычный вопрос «Да/Нет»: отменить это действие нечем, а
«Да» в диалоге нажимается не глядя. Здесь слово набирается руками, и пока оно
не набрано, кнопка неактивна — промахом мыши такое не запускается.

Пароль спрашивается вторым полем и проверяется сервером. Токен для этого
недостаточен: он живёт двенадцать часов в памяти открытого приложения, и
незапертый компьютер администратора не должен означать пустую базу.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ...core.payments.admin import CONFIRM, Scope
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle


class WipeDialog(QDialog):
    """Слово подтверждения и пароль администратора."""

    def __init__(self, scope: Scope, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scope = scope
        self.setWindowTitle("Удаление данных")
        self.setModal(True)
        self.setMinimumWidth(480)
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(SectionTitle("Удаление данных общей базы"))

        self.scope_text = QLabel(f"Будет стёрто: {self.scope.summary}.", self)
        self.scope_text.setWordWrap(True)
        self.scope_text.setStyleSheet(
            f"color: {Palette.DANGER}; font-weight: 600;")
        root.addWidget(self.scope_text)

        root.addWidget(Hint(
            "Вместе с оплатами удаляются их вложения, бюджеты, журнал импортов "
            "и привязки получателей к карточкам поставщиков. Данные стираются "
            "у всех сразу и восстанавливаются только из ночной резервной копии "
            "сервера."))
        root.addWidget(Hint(
            "Остаются учётные записи, направления, закрепление поставщиков за "
            "менеджерами и журнал изменений — в него попадёт и эта запись. "
            "После удаления выгрузку из 1С можно импортировать заново."))

        form = QFormLayout()
        form.setSpacing(Metrics.GAP)

        self.confirm = QLineEdit(self)
        self.confirm.setPlaceholderText(CONFIRM)
        self.confirm.textChanged.connect(self._recheck)
        form.addRow(f"Наберите {CONFIRM}", self.confirm)

        self.password = QLineEdit(self)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("пароль вашей учётной записи")
        self.password.textChanged.connect(self._recheck)
        form.addRow("Ваш пароль", self.password)
        root.addLayout(form)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.accept_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.accept_button.setText("Удалить данные")
        self.accept_button.setObjectName("Danger")
        self.accept_button.setEnabled(False)
        cancel = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText("Отмена")
        # Отмена под фокусом: Enter, нажатый по привычке, закрывает окно, а не
        # стирает базу.
        cancel.setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self.confirm.setFocus()

    def _recheck(self) -> None:
        self.accept_button.setEnabled(
            self.confirm.text().strip().upper() == CONFIRM
            and bool(self.password.text()))

    def result_password(self) -> str:
        return self.password.text()
