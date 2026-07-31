"""Вход в общую базу оплат.

Модальный диалог: пока вход не выполнен, показывать экран оплат нечего. Отказ
от входа не закрывает приложение — остальные разделы (заказы, переоценка,
поставщики) работают со своими данными и сервер им не нужен.

Вход выполняется в фоновом потоке: проверка пароля на сервере занимает около
сотой доли секунды, но канал до него может оказаться и медленным, а замерший
на это время интерфейс выглядит как зависшая программа.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ...core.payments import transport
from ...core.settings import AppSettings
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle


class _SignIn(QThread):
    """Один вход. Поток живёт ровно до ответа сервера."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, url: str, login: str, password: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._url, self._login, self._password = url, login, password

    def run(self) -> None:
        try:
            session = transport.sign_in(self._url, self._login, self._password)
        except transport.ServerError as error:
            self.failed.emit(str(error))
        except Exception as error:  # noqa: BLE001 — окно не должно падать молча
            self.failed.emit(f"Непредвиденная ошибка: {error}")
        else:
            self.done.emit(session)


class LoginDialog(QDialog):
    """Логин, пароль и адрес сервера."""

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._worker: _SignIn | None = None
        self.setWindowTitle("Вход в общую базу оплат")
        self.setModal(True)
        self.setMinimumWidth(420)
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(SectionTitle("Оплаты поставщикам"))
        root.addWidget(Hint("Общая база: оплаты видят все категорийные "
                            "менеджеры, править можно только свои."))

        form = QFormLayout()
        form.setSpacing(Metrics.GAP)

        self.login = QLineEdit(self.settings.payment_login)
        self.login.setPlaceholderText("например, e.ivanov")
        form.addRow("Логин", self.login)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Пароль", self.password)

        self.server = QLineEdit(self.settings.payment_server)
        self.server.setPlaceholderText("https://retail.qbeely.ru")
        form.addRow("Сервер", self.server)

        root.addLayout(form)

        self.remember = QCheckBox("Запомнить логин")
        self.remember.setChecked(bool(self.settings.payment_login))
        root.addWidget(self.remember)

        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setStyleSheet(f"color: {Palette.DANGER};")
        self.message.hide()
        root.addWidget(self.message)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(
            QDialogButtonBox.StandardButton.Ok).setText("Войти")
        self.buttons.button(
            QDialogButtonBox.StandardButton.Cancel).setText("Работать без входа")
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        # Enter в любом поле означает «войти», это привычнее, чем искать кнопку.
        for field in (self.login, self.password, self.server):
            field.returnPressed.connect(self._submit)

        (self.password if self.settings.payment_login else self.login).setFocus()

    def _submit(self) -> None:
        login = self.login.text().strip()
        password = self.password.text()
        server = self.server.text().strip()
        if not login or not password:
            self._show_error("Заполните логин и пароль")
            return
        if not server.lower().startswith("https://"):
            # Пароль уходит в теле запроса: по http его прочитает любой, кто
            # окажется между офисом и сервером.
            self._show_error("Адрес сервера должен начинаться с https://")
            return

        self._busy(True)
        self._worker = _SignIn(server, login, password, self)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _busy(self, busy: bool) -> None:
        self.buttons.setEnabled(not busy)
        for field in (self.login, self.password, self.server):
            field.setEnabled(not busy)
        self.setCursor(Qt.CursorShape.WaitCursor if busy
                       else Qt.CursorShape.ArrowCursor)

    def _show_error(self, text: str) -> None:
        self.message.setText(text)
        self.message.show()

    def _on_done(self, session: transport.Session) -> None:
        self.settings.payment_server = session.base_url
        self.settings.payment_login = session.login if self.remember.isChecked() else ""
        self.settings.save()
        self._busy(False)
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._busy(False)
        self.password.clear()
        self.password.setFocus()
        self._show_error(message)


def ensure_session(settings: AppSettings, parent: QWidget | None = None) -> bool:
    """Возвращает True, если вход выполнен. Спрашивает, только если нужно."""
    if transport.session.active:
        return True
    if LoginDialog(settings, parent).exec() != QDialog.DialogCode.Accepted:
        return False

    if transport.session.must_change_password:
        # Пароль выдан администратором. Отказ от замены — это отказ от входа:
        # иначе требование стало бы предложением, которое закрывают крестиком.
        from .password_dialog import PasswordDialog

        if PasswordDialog(required=True, parent=parent).exec() != \
                QDialog.DialogCode.Accepted:
            transport.sign_out()
            return False
        transport.session.must_change_password = False
    return True
