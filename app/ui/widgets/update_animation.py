"""Небольшая анимация загрузки из моноширинных символов."""
from __future__ import annotations

import sys

from PySide6.QtCore import QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from ..theme import Palette


class UpdateAnimation(QWidget):
    """Рисует ноутбук из символов; движение задаёт таймер Qt."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(220)
        self.setAccessibleName("Анимация загрузки обновления RetailCore")
        self._playing = False
        self._frame = 0
        self.version = ""
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._advance)

    @property
    def is_playing(self) -> bool:
        return self._playing

    def start(self) -> None:
        self._playing = True
        if self.isVisible():
            self._timer.start()

    def stop(self) -> None:
        self._playing = False
        self._timer.stop()
        self._frame = 0

    def set_version(self, version: str) -> None:
        self.version = version
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._playing:
            self._timer.start()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def _advance(self) -> None:
        self._frame = (self._frame + 1) % 60
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.translate(self.width() / 2, 0)

        # Тень, боковая грань экрана и передний торец придают рисунку глубину.
        shadow = QColor(Palette.TEXT_FAINT)
        shadow.setAlpha(35)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow)
        painter.drawEllipse(QRectF(-143, 196, 286, 12))

        painter.setBrush(QColor(Palette.BORDER_STRONG))
        painter.drawPolygon(QPolygonF([
            QPointF(112, 22), QPointF(122, 30),
            QPointF(122, 158), QPointF(112, 151),
        ]))
        painter.setPen(QPen(QColor(Palette.BORDER_STRONG), 1))
        painter.setBrush(QColor(Palette.SURFACE_ALT))
        painter.drawRoundedRect(QRectF(-112, 22, 224, 130), 5, 5)
        painter.setBrush(QColor(Palette.SURFACE))
        painter.drawRect(QRectF(-104, 30, 208, 112))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(Palette.BORDER_STRONG))
        painter.drawPolygon(QPolygonF([
            QPointF(-140, 185), QPointF(140, 185),
            QPointF(134, 194), QPointF(-134, 194),
        ]))
        painter.setPen(QPen(QColor(Palette.BORDER_STRONG), 1))
        painter.setBrush(QColor(Palette.SURFACE_ALT))
        painter.drawPolygon(QPolygonF([
            QPointF(-112, 152), QPointF(122, 152),
            QPointF(140, 185), QPointF(-140, 185),
        ]))

        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPixelSize(8)
        painter.setFont(font)

        faint = QColor(Palette.TEXT_FAINT)
        faint.setAlpha(140)
        mid = QColor(Palette.TEXT_MUTED)
        mid.setAlpha(170)
        accent = QColor(Palette.PRIMARY)
        accent.setAlpha(170)

        def line(x: int, y: int, text: str, color: QColor = faint) -> None:
            painter.setPen(color)
            painter.drawText(x, y, text)

        # Символьная фактура оставляет рисунок близким к образцу.
        line(-107, 25, ".::::::::::::::::::::::::::::::::::::::::.", mid)
        line(-107, 150, "'::::::::::::::::::::::::::::::::::::::::'", mid)
        for row in range(4):
            y = 161 + row * 6
            x = -106 - row * 8
            shimmer = (self._frame // 2) % 4 == row
            line(x, y, ": : : : : : : : : : : : : : : : : : : : : :", accent if shimmer else faint)
        line(-91, 188, "':::::::::::::::::::::::::::::::::::'", mid)
        line(-117, 47, ":|", mid)
        line(105, 47, "|:", mid)
        line(-117, 137, ":|", mid)
        line(105, 137, "|:", mid)
        line(113, 47, "\\:", faint)
        line(113, 137, "\\:", faint)

        # Блик проходит по экрану; номер берётся из загружаемого релиза.
        scan_y = 37 + self._frame * 100 / 60
        glow = QColor(Palette.PRIMARY_SOFT)
        glow.setAlpha(100)
        painter.fillRect(QRectF(-103, scan_y, 206, 4), glow)
        painter.setPen(QColor(Palette.TEXT))
        painter.setFont(QFont("Menlo" if sys.platform == "darwin" else "Consolas", 13, QFont.Weight.DemiBold))
        painter.drawText(QRectF(-99, 56, 198, 29), Qt.AlignmentFlag.AlignCenter, "RetailCore")
        painter.setFont(QFont("Menlo" if sys.platform == "darwin" else "Consolas", 10))
        painter.drawText(QRectF(-99, 83, 198, 23), Qt.AlignmentFlag.AlignCenter,
                         f"v{self.version}" if self.version else "")
        painter.setFont(font)
        dots = "." * (1 + self._frame // 5 % 3)
        painter.setPen(accent)
        painter.drawText(QRectF(-99, 111, 198, 18), Qt.AlignmentFlag.AlignCenter, f"UPDATE {dots}")
        painter.end()
