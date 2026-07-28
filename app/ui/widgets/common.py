"""Базовые элементы оформления: карточки, тени, заголовки, анимация появления."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import Metrics, Palette


class Card(QFrame):
    """Белая карточка со скруглением и мягкой тенью."""

    def __init__(self, parent: QWidget | None = None, padding: int = Metrics.PAD, shadow: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(padding, padding, padding, padding)
        layout.setSpacing(Metrics.GAP)
        if shadow:
            apply_shadow(self)

    def body(self) -> QVBoxLayout:
        return self.layout()


def apply_shadow(widget: QWidget, blur: int = 24, alpha: int = 26, offset: int = 3) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setColor(QColor(15, 23, 42, alpha))
    effect.setOffset(0, offset)
    widget.setGraphicsEffect(effect)


def fade_in(widget: QWidget, duration: int = 220) -> QPropertyAnimation:
    """Плавное появление виджета. Ссылка на анимацию хранится на виджете."""
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(duration)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    animation.finished.connect(lambda: widget.setGraphicsEffect(None))
    animation.start(QPropertyAnimation.DeletionPolicy.KeepWhenStopped)
    widget._fade_animation = animation  # предотвращает сборку мусора
    return animation


class Title(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("PageTitle")


class Subtitle(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("PageSubtitle")
        self.setWordWrap(True)


class SectionTitle(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("SectionTitle")


class Hint(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Hint")
        self.setWordWrap(True)


class Divider(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class MetricTile(QFrame):
    """Плитка со счётчиком: значение крупно, подпись мелко, цветная полоса слева."""

    def __init__(self, label: str, color: str = Palette.PRIMARY, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._color = color
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._bar = QFrame(self)
        self._bar.setFixedWidth(4)
        self._bar.setStyleSheet(
            f"background: {color}; border-top-left-radius: {Metrics.RADIUS}px;"
            f" border-bottom-left-radius: {Metrics.RADIUS}px;"
        )
        layout.addWidget(self._bar)

        inner = QVBoxLayout()
        inner.setContentsMargins(14, 10, 14, 10)
        inner.setSpacing(1)
        self._value = QLabel("0", self)
        self._value.setObjectName("Metric")
        self._value.setStyleSheet(f"color: {color};")
        self._label = QLabel(label, self)
        self._label.setObjectName("MetricLabel")
        inner.addWidget(self._value)
        inner.addWidget(self._label)
        layout.addLayout(inner)
        layout.addStretch(1)

    def set_value(self, value: int | str) -> None:
        self._value.setText(str(value))


class Badge(QLabel):
    """Небольшая цветная плашка для статусов."""

    def __init__(self, text: str = "", color: str = Palette.PRIMARY, background: str = Palette.PRIMARY_SOFT,
                 parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_colors(color, background)

    def set_colors(self, color: str, background: str) -> None:
        self.setStyleSheet(
            f"color: {color}; background: {background}; border-radius: {Metrics.RADIUS_SM}px;"
            f" padding: 3px 9px; font-size: 12px; font-weight: 600;"
        )
