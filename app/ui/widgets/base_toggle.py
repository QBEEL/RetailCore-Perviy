"""Переключатель «Общая база / Локальная база» — сегментированный, с анимацией.

Раньше это была кнопка с выпадающим меню: по виду обычная кнопка, а по сути
двухпозиционный переключатель — несоответствие формы и смысла, которое и
делало разметку «неправильной». Здесь то же самое состояние показано как
настоящий toggle: бегунок плавно едет между позициями и меняет цвет — синий
(общая, обычный режим) на жёлтый (локальная, временный режим, из которого
нужно выйти выгрузкой).
"""
from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    Qt,
    Property,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPaintEvent, QPainter, QPainterPath
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..theme import Palette


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_rgb(c1: QColor, c2: QColor, t: float) -> QColor:
    return QColor(
        int(_lerp(c1.red(), c2.red(), t)),
        int(_lerp(c1.green(), c2.green(), t)),
        int(_lerp(c1.blue(), c2.blue(), t)),
    )


def _lerp_hue(c1: QColor, c2: QColor, t: float) -> QColor:
    """Интерполяция по HSV кратчайшим путём по оттенку.

    Прямая линейная интерполяция RGB между синим и оранжевым проходит через
    серую середину — цвет на секунду «гаснет». По HSV бегунок вместо этого
    идёт через фиолетовый/пурпурный на полной насыщенности, и переход
    остаётся ярким на всём пути, а не только в начале и конце.
    """
    h1, s1, v1, _ = c1.getHsvF()
    h2, s2, v2, _ = c2.getHsvF()
    delta = h2 - h1
    if delta > 0.5:
        delta -= 1.0
    elif delta < -0.5:
        delta += 1.0
    hue = (h1 + delta * t) % 1.0
    color = QColor()
    color.setHsvF(hue, _lerp(s1, s2, t), _lerp(v1, v2, t))
    return color


class BaseToggle(QWidget):
    """Сегментированный переключатель источника оплат."""

    toggled = Signal(bool)  # True — выбрана локальная база

    _LABELS = ("Общая", "Локальная")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._local = False
        self._progress = 0.0  # 0 — общая, 1 — локальная
        self._color_shared = QColor(Palette.PRIMARY)
        self._color_local = QColor(Palette.WARNING)

        self.setFixedHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._animation = QPropertyAnimation(self, b"progress", self)
        self._animation.setDuration(220)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    # --- анимируемое свойство ------------------------------------------------
    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = value
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    # --- публичное API ---------------------------------------------------------
    def set_local(self, local: bool, animate: bool = True) -> None:
        """Переставляет бегунок. `animate=False` — мгновенно, для первой отрисовки."""
        if animate and local == self._local:
            return
        self._local = local
        target = 1.0 if local else 0.0
        self._animation.stop()
        if animate:
            self._animation.setStartValue(self._progress)
            self._animation.setEndValue(target)
            self._animation.start()
        else:
            self._set_progress(target)

    def is_local(self) -> bool:
        return self._local

    # --- ввод --------------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.width() > 0:
            local = event.position().x() > self.width() / 2
            if local != self._local:
                self.set_local(local)
                self.toggled.emit(local)
        super().mousePressEvent(event)

    # --- отрисовка ------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2

        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        painter.fillPath(track, QColor(Palette.SURFACE_ALT))
        painter.setPen(QColor(Palette.BORDER))
        painter.drawPath(track)

        margin = 3.0
        half = rect.width() / 2
        pill_rect = QRectF(
            rect.left() + margin + half * self._progress,
            rect.top() + margin,
            half - margin * 1.5,
            rect.height() - margin * 2,
        )
        pill_color = _lerp_hue(self._color_shared, self._color_local, self._progress)
        pill = QPainterPath()
        pill.addRoundedRect(pill_rect, pill_rect.height() / 2, pill_rect.height() / 2)
        painter.fillPath(pill, pill_color)

        font = QFont(self.font())
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)

        shared_rect = QRectF(rect.left(), rect.top(), half, rect.height())
        local_rect = QRectF(rect.left() + half, rect.top(), half, rect.height())
        painter.setPen(self._text_color(0.0))
        painter.drawText(shared_rect, Qt.AlignmentFlag.AlignCenter, self._LABELS[0])
        painter.setPen(self._text_color(1.0))
        painter.drawText(local_rect, Qt.AlignmentFlag.AlignCenter, self._LABELS[1])

    def _text_color(self, side: float) -> QColor:
        """Подпись под бегунком — белая, остальная — приглушённая; переход плавный."""
        active = max(0.0, 1.0 - abs(self._progress - side) * 2)
        return _lerp_rgb(QColor(Palette.TEXT_MUTED), QColor(Palette.TEXT_ON_PRIMARY), active)
