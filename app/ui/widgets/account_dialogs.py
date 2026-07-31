"""Карточка учётной записи и показ выданного пароля."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.payments.admin import Account
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle


class PasswordShown(QDialog):
    """Пароль показывается один раз — в базе остаётся только хеш."""

    def __init__(self, login: str, password: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Пароль выдан")
        self.setModal(True)
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(SectionTitle(f"Учётная запись {login}"))
        root.addWidget(Hint(
            "Запишите пароль сейчас — увидеть его повторно нельзя. "
            "При первом входе владелец должен будет его заменить."))

        row = QHBoxLayout()
        field = QLineEdit(password)
        field.setReadOnly(True)
        field.setObjectName("Path")
        field.selectAll()
        row.addWidget(field, 1)

        copy = QPushButton("Копировать", self)
        copy.setObjectName("Ghost")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(password))
        row.addWidget(copy)
        root.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Записал")
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)


class AccountDialog(QDialog):
    """Логин, имя, права и список имён из 1С."""

    def __init__(self, account: Account, known_responsible: list[str],
                 *, is_self: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.account = account
        self.is_self = is_self
        self.setWindowTitle("Учётная запись" if account.id else "Новая учётная запись")
        self.setMinimumWidth(560)
        self._build(known_responsible)

    def _build(self, known_responsible: list[str]) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        form = QFormLayout()
        form.setSpacing(Metrics.GAP)

        self.login = QLineEdit(self.account.login)
        self.login.setPlaceholderText("например, e.ivanov")
        # Логин — ключ учётки, по нему человек входит. Менять его у заведённой
        # записи нельзя: сервер такого не умеет, и обещать это в окне не нужно.
        self.login.setEnabled(not self.account.id)
        form.addRow("Логин", self.login)

        self.full_name = QLineEdit(self.account.full_name)
        self.full_name.setPlaceholderText("Фамилия Имя")
        form.addRow("ФИО", self.full_name)

        root.addLayout(form)

        self.is_admin = QCheckBox("Администратор: управляет учётками, "
                                  "бюджетами и импортом", self)
        self.is_admin.setChecked(self.account.is_admin)
        root.addWidget(self.is_admin)

        self.is_active = QCheckBox("Вход разрешён", self)
        self.is_active.setChecked(self.account.is_active)
        root.addWidget(self.is_active)

        if self.is_self:
            # Иначе единственный администратор способен разжаловать или
            # отключить сам себя, и управлять учётками станет некому.
            self.is_admin.setEnabled(False)
            self.is_active.setEnabled(False)
            root.addWidget(Hint("Это ваша учётная запись — снять с себя права "
                                "или закрыть себе вход нельзя."))

        root.addWidget(SectionTitle("Чьи оплаты считаются своими"))
        root.addWidget(Hint(
            "Отмеченные имена — так человек записан в выгрузке 1С. Оплаты с "
            "этим ответственным он сможет править. Один человек встречается "
            "под несколькими написаниями — отметьте все."))

        self.names = QListWidget(self)
        self.names.setMinimumHeight(220)
        chosen = set(self.account.responsible)
        # Уже отмеченные — наверх: иначе выбранное имя теряется в списке из
        # полусотни, и непонятно, отмечено ли вообще что-нибудь.
        ordered = sorted(set(known_responsible) | chosen,
                         key=lambda name: (name not in chosen, name))
        for name in ordered:
            item = QListWidgetItem(name, self.names)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in chosen
                               else Qt.CheckState.Unchecked)
        root.addWidget(self.names)

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

    def _accept(self) -> None:
        login = self.login.text().strip().lower()
        if not login:
            self._fail("Укажите логин")
            return
        if not login.replace(".", "").replace("-", "").replace("_", "").isalnum():
            self._fail("Логин — латиница, цифры, точка, дефис и подчёркивание")
            return
        if not self.full_name.text().strip():
            self._fail("Укажите ФИО — оно подставляется автором в новые оплаты")
            return
        self.accept()

    def _fail(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()

    def result_account(self) -> Account:
        chosen = [self.names.item(row).text()
                  for row in range(self.names.count())
                  if self.names.item(row).checkState() == Qt.CheckState.Checked]
        self.account.login = self.login.text().strip().lower()
        self.account.full_name = self.full_name.text().strip()
        self.account.responsible = chosen
        self.account.is_admin = self.is_admin.isChecked()
        self.account.is_active = self.is_active.isChecked()
        return self.account
