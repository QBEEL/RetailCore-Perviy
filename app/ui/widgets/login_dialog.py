"""Вход в приложение.

Работать без входа нельзя: программа знает, кто вносит правки в общие оплаты и
за кем закреплены поставщики, и «неизвестный пользователь» ей не подходит.
Окно показывается до главного, и отказ от входа означает выход из программы.

Исключение одно — сервер недоступен. Сопоставление, заказ и переоценка читают
Excel и локальные базы, и останавливать их из-за чужой аварии незачем. Кто
входил на этой машине недавно, продолжит работать без сети: разделы, которым
сервер нужен, закроются, остальные останутся.

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

from ...core.payments import session_store, transport
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

    # Отдельный код возврата: «продолжить без сервера» — это не согласие и не
    # отказ, а третий исход, и отличать его от закрытия окна обязательно.
    OFFLINE = QDialog.DialogCode.Accepted + 100

    def __init__(self, settings: AppSettings, parent: QWidget | None = None,
                 *, offline_days: int = 0) -> None:
        super().__init__(parent)
        self.settings = settings
        self._worker: _SignIn | None = None
        # Сколько дней осталось работать без сервера. Ноль — нисколько, и
        # кнопка «продолжить без сервера» не появится вовсе.
        self._offline_days = offline_days
        self.setWindowTitle("Вход — RetailCore")
        self.setModal(True)
        self.setMinimumWidth(440)
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(SectionTitle("RetailCore"))
        root.addWidget(Hint(
            "Общая база отдела: оплаты, поставщики и отчётность. Программа "
            "должна знать, кто вносит правки и за кем закреплены поставщики, "
            "поэтому вход обязателен."))

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
        # Не «отмена», а «выйти»: отказ от входа закрывает программу, и кнопка
        # обязана говорить об этом прямо.
        self.buttons.button(
            QDialogButtonBox.StandardButton.Cancel).setText("Выйти из программы")
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)

        if self._offline_days:
            offline = self.buttons.addButton(
                "Продолжить без сервера",
                QDialogButtonBox.ButtonRole.DestructiveRole)
            offline.setToolTip(
                f"Оплаты, поставщики и отчётность будут закрыты.\n"
                f"Сопоставление, заказ и переоценка работают как обычно.\n"
                f"Осталось дней без входа: {self._offline_days}")
            offline.clicked.connect(lambda: self.done(self.OFFLINE))
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
        # Токен запоминается всегда, независимо от «запомнить логин»: тот
        # флажок про подстановку имени в поле, а не про повторный ввод пароля
        # при каждом перезапуске. Пароль не сохраняется ни при каком выборе.
        session_store.save(session)
        self._busy(False)
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._busy(False)
        self.password.clear()
        self.password.setFocus()
        self._show_error(message)


class Start:
    """Чем закончился запуск: работаем, работаем без сервера или выходим."""

    ONLINE = "online"
    OFFLINE = "offline"
    QUIT = "quit"


def start_session(settings: AppSettings, parent: QWidget | None = None) -> str:
    """Вход при запуске программы. Вызывается до создания главного окна.

    Сохранённый токен поднимается молча: пароль спрашивается раз в двенадцать
    часов, а не при каждом запуске — программу закрывают и открывают вместе с
    очередным файлом, и десять паролей в день никто вводить не станет.
    """
    saved = session_store.load()
    if saved.valid:
        transport.restore(saved)
        return Start.ONLINE

    outcome = LoginDialog(settings, parent,
                          offline_days=saved.grace_left()).exec()
    if outcome == LoginDialog.OFFLINE:
        return Start.OFFLINE
    if outcome != QDialog.DialogCode.Accepted:
        return Start.QUIT
    return Start.ONLINE if _password_settled(parent) else Start.QUIT


def sign_out() -> None:
    """Выход из учётной записи: и в этом запуске, и в сохранённом профиле."""
    transport.sign_out()
    session_store.forget()


def ensure_session(settings: AppSettings, parent: QWidget | None = None) -> bool:
    """Возвращает True, если вход выполнен. Спрашивает, только если нужно.

    Остаётся для случаев, когда сессия отвалилась посреди работы: токен истёк
    или программу запустили без сервера, а он появился.
    """
    if transport.session.active:
        return True
    saved = session_store.load()
    if saved.valid:
        transport.restore(saved)
        return True
    if LoginDialog(settings, parent).exec() != QDialog.DialogCode.Accepted:
        return False
    return _password_settled(parent)


def _password_settled(parent: QWidget | None) -> bool:
    """Выданный администратором пароль обязан быть заменён при первом входе."""
    if transport.session.must_change_password:
        # Пароль выдан администратором. Отказ от замены — это отказ от входа:
        # иначе требование стало бы предложением, которое закрывают крестиком.
        from .password_dialog import PasswordDialog

        if PasswordDialog(required=True, parent=parent).exec() != \
                QDialog.DialogCode.Accepted:
            sign_out()
            return False
        transport.session.must_change_password = False
        # Смена пароля выдаёт новый токен: сохранённый рядом устарел бы и
        # первый же запуск потребовал бы пароль заново.
        session_store.save(transport.session)
    return True
