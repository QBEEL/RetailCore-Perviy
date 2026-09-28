"""Закрытая вкладка: затемнение, анимация и объяснение поверх содержимого.

Раньше закрытый отчёт просто пропадал из списка вкладок. Человек, которому о
нём рассказали коллеги, искал его и не находил — и решал, что у него старая
версия программы. Теперь вкладка на месте, но всё под ней недоступно, а
поверх сказано, что делать: идти к администратору.

Содержимое выключается целиком, а не только прикрывается сверху: затемнение
перехватывает мышь, но до кнопок под ним можно добраться клавишей Tab.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QMovie, QPainter
from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from ..resources import asset
from ..theme import Metrics

# Строки разбиты заранее, а не переносом: подпись с переносом, выровненная по
# центру, считает высоту по первой строке и теряет последнюю.
MESSAGE = ("Вход в этот отчёт для Вас закрыт\n"
           "или находится на доработке.\n"
           "Пожалуйста, обратитесь к администратору")

# Затемнение: достаточно, чтобы читалось «сюда нельзя», но не настолько, чтобы
# вкладка выглядела пустой и сломанной — очертания отчёта под ним видны.
SHADE = QColor(15, 23, 42, 185)
ANIMATION_WIDTH = 220


class LockedOverlay(QWidget):
    """Затемнение с анимацией и сообщением. Анимация идёт, только пока видно."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        layout.setSpacing(Metrics.GAP)
        layout.addStretch(1)

        self.animation = QLabel(self)
        self.animation.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.movie = QMovie(str(asset("locked.gif")), parent=self)
        size = self.movie.frameRect().size()
        if size.isValid() and size.width():
            height = round(size.height() * ANIMATION_WIDTH / size.width())
            self.movie.setScaledSize(QSize(ANIMATION_WIDTH, height))
        self.animation.setMovie(self.movie)
        layout.addWidget(self.animation, 0, Qt.AlignmentFlag.AlignHCenter)

        self.message = QLabel(MESSAGE, self)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setStyleSheet(
            "color: #ffffff; font-size: 15px; font-weight: 600; background: transparent;")
        layout.addWidget(self.message, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.fillRect(self.rect(), SHADE)
        painter.end()

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        self.movie.start()

    def hideEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        # Двадцать кадров в секунду на скрытой вкладке — впустую занятый
        # процессор у человека, который в этот момент работает в другой.
        self.movie.stop()
        super().hideEvent(event)


class Lockable(QWidget):
    """Обёртка вкладки: содержимое и затемнение в одной клетке, затемнение сверху."""

    def __init__(self, content: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.content = content
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(content, 0, 0)
        self.overlay = LockedOverlay(self)
        layout.addWidget(self.overlay, 0, 0)
        self.overlay.hide()

    @property
    def locked(self) -> bool:
        return not self.overlay.isHidden()

    def set_locked(self, locked: bool) -> None:
        self.content.setEnabled(not locked)
        self.overlay.setVisible(locked)
        if locked:
            self.overlay.raise_()
