"""Значок программы в области уведомлений Windows.

Пока окно спрятано в трей, программа продолжает работать: следит за заявками
Торг-2 и показывает о них уведомление. Значок — единственный способ вернуть
окно и единственный честный способ выйти, поэтому прятать окно можно только
при видимом значке (см. `MainWindow.closeEvent`).
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from .. import APP_TITLE
from . import icons


class AppTray(QObject):
    open_requested = Signal()
    quit_requested = Signal()
    message_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shown = False
        self._icon = QSystemTrayIcon(icons.app_icon(), self)
        self._icon.setToolTip(APP_TITLE)
        # Меню без родителя-виджета Qt не удерживает: ссылка нужна, иначе его
        # соберёт сборщик мусора, и по правому щелчку не откроется ничего.
        self._menu = QMenu()
        self._menu.addAction(f"Открыть {APP_TITLE}", self.open_requested.emit)
        self._menu.addSeparator()
        self._menu.addAction("Выйти", self.quit_requested.emit)
        self._icon.setContextMenu(self._menu)
        self._icon.activated.connect(self._activated)
        self._icon.messageClicked.connect(self.message_clicked)

    @staticmethod
    def supported() -> bool:
        """Сворачивание в трей — привычка Windows. На macOS программа остаётся в Dock."""
        return sys.platform.startswith("win")

    @classmethod
    def available(cls) -> bool:
        return cls.supported() and QSystemTrayIcon.isSystemTrayAvailable()

    @property
    def visible(self) -> bool:
        return self._shown

    def show(self) -> None:
        self._icon.show()
        self._shown = True

    def hide(self) -> None:
        self._icon.hide()
        self._shown = False

    def message(self, title: str, text: str, milliseconds: int = 10_000) -> None:
        self._icon.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information,
                               milliseconds)

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_requested.emit()
