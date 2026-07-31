"""Администрирование: учётные записи и журнал изменений.

Раздел виден только администратору и только при работе с общей базой. Без входа
на сервер управлять нечем — учётные записи существуют там, а не в приложении.

Всё, что требует сети, уходит в фоновую задачу: сервер за границей офиса, и
замерший на секунду интерфейс выглядит как зависшая программа.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.payments import admin, data, transport
from ..core.payments.admin import Account, Entry
from ..core.settings import AppSettings
from . import icons
from .tasks import run_task
from .theme import Metrics, Palette
from .widgets.account_dialogs import AccountDialog, PasswordShown
from .widgets.common import Card, Hint, SectionTitle, Subtitle, Title
from .widgets.table import Column, DataTable
from .widgets.toast import ToastKind


def _moment(value: datetime | None) -> str:
    return f"{value:%d.%m.%Y %H:%M}" if value else ""


class AdminPage(QWidget):
    """Страница администрирования."""

    def __init__(self, settings: AppSettings, notify, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self.accounts: list[Account] = []
        self.entries: list[Entry] = []
        self._known: list[str] = []
        self._loaded = False

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 8, Metrics.PAD + 4,
                                Metrics.PAD + 8, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(Title("Администрирование", self))
        self.subtitle = Subtitle("Учётные записи и журнал изменений общей базы", self)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)

        self.refresh_button = QPushButton("Обновить", self)
        self.refresh_button.setIcon(icons.icon("refresh"))
        self.refresh_button.clicked.connect(self.reload)
        head.addWidget(self.refresh_button)

        self.new_button = QPushButton("Новая учётная запись", self)
        self.new_button.setObjectName("Primary")
        self.new_button.setIcon(icons.icon("plus"))
        self.new_button.clicked.connect(self.create_account)
        head.addWidget(self.new_button)
        root.addLayout(head)

        self.offline = Card(self)
        self.offline.body().addWidget(SectionTitle("Нет связи с общей базой"))
        self.offline.body().addWidget(Hint(
            "Учётные записи хранятся на сервере. Откройте раздел «Оплаты» и "
            "войдите — после этого управление станет доступно."))
        root.addWidget(self.offline)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._accounts_tab(), "Учётные записи")
        self.tabs.addTab(self._journal_tab(), "Журнал изменений")
        root.addWidget(self.tabs, 1)

    # --- вкладки --------------------------------------------------------------

    def _accounts_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, Metrics.GAP, 0, 0)
        layout.setSpacing(Metrics.GAP)

        self.accounts_table = DataTable([
            Column("ФИО", lambda a: a.title, 220),
            Column("Логин", lambda a: a.login, 140),
            Column("Роль", lambda a: a.role, 130,
                   color=lambda a: None if a.is_active else QColor(Palette.TEXT_FAINT)),
            Column("Имён в 1С", lambda a: len(a.responsible), 90,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            Column("Кто в 1С", lambda a: ", ".join(a.responsible), 320),
            Column("Заведена", lambda a: _moment(a.created_at), 130,
                   sort_key=lambda a: a.created_at or datetime.min),
        ], page)
        self.accounts_table.item_activated.connect(self.edit_account)
        layout.addWidget(self.accounts_table, 1)

        actions = QHBoxLayout()
        self.edit_button = QPushButton("Изменить", page)
        self.edit_button.setIcon(icons.icon("edit"))
        self.edit_button.clicked.connect(lambda: self.edit_account(self._chosen()))
        actions.addWidget(self.edit_button)

        self.reset_button = QPushButton("Назначить новый пароль", page)
        self.reset_button.setObjectName("Ghost")
        self.reset_button.setIcon(icons.icon("key"))
        self.reset_button.clicked.connect(self.reset_password)
        actions.addWidget(self.reset_button)

        actions.addStretch(1)
        self.hint = QLabel("", page)
        self.hint.setObjectName("Hint")
        actions.addWidget(self.hint)
        layout.addLayout(actions)
        return page

    def _journal_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, Metrics.GAP, 0, 0)
        layout.setSpacing(Metrics.GAP)

        layout.addWidget(Hint(
            "Двести последних действий. Массовый импорт из 1С даёт одну запись "
            "на прогон, а не на строку."))

        self.journal_table = DataTable([
            Column("Когда", lambda e: _moment(e.at), 140,
                   sort_key=lambda e: e.at or datetime.min),
            Column("Кто", lambda e: e.user, 190),
            Column("Что", lambda e: e.entity, 150),
            Column("Действие", lambda e: e.action, 150),
            Column("Запись", lambda e: e.entity_id or "", 80,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            Column("Изменения", lambda e: e.summary, 420),
        ], page)
        layout.addWidget(self.journal_table, 1)
        return page

    # --- загрузка -------------------------------------------------------------

    def restore(self) -> None:
        if self._loaded:
            return
        self.reload()

    def reload(self) -> None:
        available = transport.session.active and transport.session.is_admin
        self.offline.setVisible(not available)
        self.tabs.setVisible(available)
        for button in (self.new_button, self.refresh_button,
                       self.edit_button, self.reset_button):
            button.setEnabled(available)
        if not available:
            self.subtitle.setText("Требуется вход администратором")
            return

        run_task(_load_all, on_result=self._apply, on_error=self._failed)

    def _apply(self, payload: tuple[list[Account], list[Entry], list[str]]) -> None:
        self.accounts, self.entries, self._known = payload
        self._loaded = True
        self.accounts_table.set_items(self.accounts)
        self.journal_table.set_items(self.entries)
        active = sum(1 for a in self.accounts if a.is_active)
        admins = sum(1 for a in self.accounts if a.is_admin and a.is_active)
        self.subtitle.setText(
            f"Учётных записей: {len(self.accounts)} · с доступом: {active} · "
            f"администраторов: {admins}")
        # Сколько человек из выгрузки остались без учётки: их оплаты видны, но
        # править их некому, и это стоит замечать до того, как спросят.
        linked = {name for account in self.accounts for name in account.responsible}
        self.hint.setText(f"без учётной записи в 1С: {len(set(self._known) - linked)}")

    def _failed(self, message: str) -> None:
        self.notify(message, ToastKind.ERROR)

    # --- действия -------------------------------------------------------------

    def _chosen(self) -> Account | None:
        rows = self.accounts_table.selected_items()
        return rows[0] if rows else None

    def create_account(self) -> None:
        dialog = AccountDialog(Account(), self._known, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        account = dialog.result_account()
        run_task(lambda: admin.create(account),
                 on_result=self._created, on_error=self._failed)

    def _created(self, payload: tuple[Account, str]) -> None:
        account, password = payload
        PasswordShown(account.login, password, self).exec()
        self.notify(f"Учётная запись {account.login} заведена", ToastKind.SUCCESS)
        self.reload()

    def edit_account(self, account: Account | None) -> None:
        if account is None:
            self.notify("Выберите учётную запись", ToastKind.INFO)
            return
        dialog = AccountDialog(account, self._known,
                               is_self=account.login == transport.session.login,
                               parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        changed = dialog.result_account()
        run_task(lambda: admin.save(changed),
                 on_result=lambda _: self._saved(changed), on_error=self._failed)

    def _saved(self, account: Account) -> None:
        self.notify(f"Учётная запись {account.login} изменена", ToastKind.SUCCESS)
        self.reload()

    def reset_password(self) -> None:
        account = self._chosen()
        if account is None:
            self.notify("Выберите учётную запись", ToastKind.INFO)
            return
        answer = QMessageBox.question(
            self, "Назначить новый пароль",
            f"Назначить новый пароль для {account.title}?\n\n"
            "Прежний перестанет работать сразу. Новый нужно будет передать "
            "лично — при первом входе владелец заменит его на свой.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        run_task(lambda: admin.reset_password(account.id),
                 on_result=lambda password: self._reset_done(account, password),
                 on_error=self._failed)

    def _reset_done(self, account: Account, password: str) -> None:
        PasswordShown(account.login, password, self).exec()
        self.reload()


def _load_all() -> tuple[list[Account], list[Entry], list[str]]:
    """Учётки, журнал и список имён из 1С — одним походом на сервер."""
    known = data.known_values().get("responsible", [])
    return admin.accounts(), admin.journal(), known
