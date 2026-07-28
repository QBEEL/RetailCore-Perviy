"""Всплывающие уведомления вместо модальных окон."""
from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from .. import icons
from ..theme import Metrics, Palette
from .common import apply_shadow


class ToastKind(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


_STYLE: dict[ToastKind, tuple[str, str, str]] = {
    ToastKind.SUCCESS: (Palette.SUCCESS, Palette.SUCCESS_SOFT, "check"),
    ToastKind.ERROR: (Palette.DANGER, Palette.DANGER_SOFT, "error"),
    ToastKind.WARNING: (Palette.WARNING, Palette.WARNING_SOFT, "warning"),
    ToastKind.INFO: (Palette.PRIMARY, Palette.PRIMARY_SOFT, "info"),
}


class Toast(QFrame):
    """Одиночное уведомление, всплывающее в правом нижнем углу окна."""

    def __init__(self, parent: QWidget, text: str, kind: ToastKind, timeout: int = 4000) -> None:
        super().__init__(parent)
        color, background, icon_name = _STYLE[kind]
        self.setStyleSheet(
            f"QFrame {{ background: {background}; border: 1px solid {color};"
            f" border-radius: {Metrics.RADIUS}px; }}"
            f" QLabel {{ color: {color}; font-weight: 500; background: transparent; border: none; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 11, 15, 11)
        layout.setSpacing(9)

        badge = QLabel(self)
        badge.setPixmap(icons.icon(icon_name, color).pixmap(18, 18))
        badge.setFixedSize(18, 18)
        layout.addWidget(badge)

        label = QLabel(text, self)
        label.setWordWrap(True)
        label.setMaximumWidth(360)
        layout.addWidget(label, 1)

        apply_shadow(self, blur=28, alpha=38, offset=5)
        self.adjustSize()
        QTimer.singleShot(timeout, self.dismiss)

    def slide_in(self, target: QPoint) -> None:
        start = QPoint(target.x() + 28, target.y())
        self.move(start)
        self.show()
        self._animation = QPropertyAnimation(self, b"pos", self)
        self._animation.setDuration(240)
        self._animation.setStartValue(start)
        self._animation.setEndValue(target)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.start()

    def dismiss(self) -> None:
        if not self.isVisible():
            return
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(180)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._remove)
        self._fade.start()

    def _remove(self) -> None:
        manager = getattr(self.parent(), "_toast_manager", None)
        if manager is not None:
            manager.remove(self)
        self.deleteLater()

    def mousePressEvent(self, event) -> None:
        self.dismiss()


class ToastManager:
    """Складывает уведомления стопкой и пересчитывает их позиции."""

    def __init__(self, host: QWidget, margin: int = 22, spacing: int = 10) -> None:
        self.host = host
        self.margin = margin
        self.spacing = spacing
        self._toasts: list[Toast] = []
        host._toast_manager = self

    def show(self, text: str, kind: ToastKind = ToastKind.INFO, timeout: int = 4000) -> None:
        toast = Toast(self.host, text, kind, timeout)
        self._toasts.append(toast)
        self.reposition()
        toast.slide_in(toast.pos())
        toast.raise_()

    def remove(self, toast: Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
            self.reposition()

    def reposition(self) -> None:
        y = self.host.height() - self.margin
        for toast in reversed(self._toasts):
            y -= toast.height() + self.spacing
            toast.move(self.host.width() - toast.width() - self.margin, y)

    def clear(self) -> None:
        for toast in list(self._toasts):
            toast.dismiss()
