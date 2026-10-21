"""Одна копия программы на пользователя.

С окном, спрятанным в трей, второй запуск перестаёт быть редкостью: человек
не видит окна и открывает программу ещё раз. Две копии одновременно пишут одни
и те же настройки и заметки и спорят за порт туннеля, поэтому вторая копия не
стартует, а просит первую показать окно.

После самообновления новая копия запускается раньше, чем уходит старая. Её
отличает переменная окружения (см. `updater.RESTART_VARIABLE`): такая копия не
уходит, а ждёт, пока старая закроется.
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

WAIT_FOR_PREVIOUS = 15.0


def default_name() -> str:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return f"RetailCore.App.{user}"


class SingleInstance(QObject):
    activated = Signal()

    def __init__(self, name: str | None = None) -> None:
        super().__init__()
        self._name = name or default_name()
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._accepted)

    def claim(self, wait_for_previous: bool = False,
              timeout: float = WAIT_FOR_PREVIOUS) -> bool:
        """Истина — копия единственная и может работать.

        Ложь — другая уже запущена: ей отправлена просьба показать окно (если
        мы не ждём её ухода после обновления).
        """
        deadline = time.monotonic() + timeout
        while self._other_running(announce=not wait_for_previous):
            if not wait_for_previous or time.monotonic() >= deadline:
                return False
            time.sleep(0.2)
        QLocalServer.removeServer(self._name)
        # Не вышло занять имя — работаем без защиты: лучше две копии, чем ни одной.
        self._server.listen(self._name)
        return True

    def _other_running(self, announce: bool) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if not socket.waitForConnected(300):
            return False
        if announce:
            socket.write(b"show")
            socket.flush()
            # Копия сразу завершится, и сокет закроется вместе с ней: просьба
            # должна успеть уйти, иначе первая копия так и не покажет окно.
            deadline = time.monotonic() + 1.0
            while socket.bytesToWrite() and time.monotonic() < deadline:
                socket.waitForBytesWritten(50)
                QCoreApplication.processEvents()
        socket.disconnectFromServer()
        if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            socket.waitForDisconnected(300)
        return True

    def _accepted(self) -> None:
        while self._server.hasPendingConnections():
            connection = self._server.nextPendingConnection()
            # Пустое соединение — копия, ждущая нашего ухода после обновления:
            # окно по её «стуку» поднимать не нужно.
            asked = (connection.bytesAvailable() or connection.waitForReadyRead(200)) \
                and bytes(connection.readAll().data()) == b"show"
            connection.disconnectFromServer()
            connection.deleteLater()
            if asked:
                self.activated.emit()
