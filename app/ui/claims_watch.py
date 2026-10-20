"""Уведомления Торг-2: когда на человека поставили заявку.

Сервер ничего не присылает сам: приложение раз в минуту спрашивает, что
адресовано вошедшему и ещё не открыто. Запрос дешёвый (счётчик по индексу), а
двадцати запущенным приложениям это раз в минуту каждому — незаметно. Отдельный
канал push ради этого — это ещё один сервер, который нужно держать живым.

Что «уже сообщено», помнит база (`manager_seen`), а не приложение: перезапуск не
теряет непрочитанное и не показывает одно и то же дважды после открытия.
Внутри запущенного приложения сверх того запоминается, что именно показано, —
иначе одна и та же заявка всплывала бы каждую минуту, пока её не откроют.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtWidgets import QLabel, QWidget

from ..core.claims import Inbox, InboxItem, KIND_TITLES, remote
from ..core.payments import transport
from .tasks import run_task
from .theme import Palette

POLL_MS = 60_000
# Сразу после запуска не спрашиваем: окно ещё рисуется, а вход мог быть только
# что выполнен. Через несколько секунд всё готово.
FIRST_POLL_MS = 4_000


class ClaimsWatcher(QObject):
    """Следит за адресованными вошедшему заявками."""

    # Сколько заявок ждут открытия. Для цифры у пункта меню.
    unseen_changed = Signal(int)
    # Появились заявки, о которых ещё не говорили: список и общее число.
    arrived = Signal(list, int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._announced: set[int] = set()
        self._busy = False
        self._unseen = 0
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self.poll)

    @property
    def unseen(self) -> int:
        return self._unseen

    def start(self) -> None:
        self._timer.start()
        QTimer.singleShot(FIRST_POLL_MS, self.poll)

    def stop(self) -> None:
        self._timer.stop()
        self._announced.clear()
        self._set_unseen(0)

    def poll(self) -> None:
        # Без входа спрашивать некого: после выхода из учётки таймер ещё тикает.
        # Токен на исходе тоже пропускаем: сервер ответил бы 401, а на 401
        # транспорт сбрасывает вход — и человек потерял бы сессию от фоновой
        # проверки, которую не просил. Вход обновится при следующем действии.
        if self._busy or not transport.session.active or transport.session.expiring:
            return
        self._busy = True
        run_task(remote.inbox, on_result=self._got, on_error=self._failed)

    def _failed(self, _message: str) -> None:
        # Нет связи или сервер старой версии — молча: это фоновая проверка, и
        # всплывающая ошибка раз в минуту была бы хуже самой проблемы.
        self._busy = False

    def _got(self, inbox: Inbox) -> None:
        self._busy = False
        fresh = [item for item in inbox.items if item.id not in self._announced]
        self._announced.update(item.id for item in inbox.items)
        # Открытое и закрытое из списка уходит — помнить его дальше незачем.
        if not inbox.unseen:
            self._announced.clear()
        self._set_unseen(inbox.unseen)
        if fresh:
            self.arrived.emit(fresh, inbox.unseen)

    def _set_unseen(self, count: int) -> None:
        if count != self._unseen:
            self._unseen = count
            self.unseen_changed.emit(count)


def describe(items: list[InboxItem], unseen: int) -> str:
    """Текст уведомления: одна заявка — подробно, несколько — числом."""
    if len(items) == 1 and unseen <= 1:
        item = items[0]
        what = " · ".join(part for part in (item.brand, item.product) if part) or "без названия"
        return (f"Торг-2: на вас поставлена заявка — {KIND_TITLES.get(item.kind, item.kind).lower()}, "
                f"{what}. Заявил {item.applicant}.")
    return f"Торг-2: на вас поставлено заявок: {unseen}. Откройте раздел, чтобы ответить."


class NavBadge(QLabel):
    """Красная цифра в углу кнопки меню.

    Сидит дочерним виджетом кнопки и сама следит за её размером: в свёрнутом
    меню подписи нет, и цифра — единственное, что говорит о непрочитанном.
    """

    def __init__(self, button: QWidget) -> None:
        super().__init__(button)
        self._button = button
        self.setStyleSheet(
            f"background: {Palette.DANGER}; color: white; border-radius: 8px;"
            " font-size: 11px; font-weight: 700; padding: 0 5px;")
        self.setFixedHeight(16)
        self.setMinimumWidth(16)
        self.hide()
        button.installEventFilter(self)

    def set_count(self, count: int) -> None:
        self.setVisible(count > 0)
        if count > 0:
            self.setText(str(count) if count < 100 else "99+")
            self.adjustSize()
            self._place()
            self.raise_()

    def _place(self) -> None:
        self.move(self._button.width() - self.width() - 10,
                  (self._button.height() - self.height()) // 2)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._button and event.type() == QEvent.Type.Resize and self.isVisible():
            self._place()
        return False
