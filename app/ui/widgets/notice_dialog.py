"""Окно-напоминание: закрыть можно не сразу, а «больше не показывать» запоминается.

Закрытие задержано намеренно. Уведомление, которое человек закрывает не читая,
не работает: оно нужно, когда правило новое и привычка ещё не сложилась. Поэтому
кнопка закрытия оживает через несколько секунд, а крестик и Esc до этого
момента ничего не делают.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..theme import Metrics, Palette
from .common import SectionTitle

DEFAULT_DELAY = 10


class NoticeDialog(QDialog):
    """Заголовок, текст, флажок «Больше не показывать» и кнопка с отсчётом."""

    def __init__(self, title: str, text: str, *, delay: int = DEFAULT_DELAY,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(480)
        # Без кнопки «?» и без крестика в рамке: закрывается только кнопкой.
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)

        self._left = max(int(delay), 0)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 4, Metrics.PAD, Metrics.PAD + 4, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(SectionTitle(title, self))
        body = QLabel(text, self)
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setStyleSheet(f"color: {Palette.TEXT}; font-size: 13px;")
        root.addWidget(body)

        row = QHBoxLayout()
        self.dont_show = QCheckBox("Больше не показывать", self)
        row.addWidget(self.dont_show)
        row.addStretch(1)
        self.close_button = QPushButton(self)
        self.close_button.setObjectName("Primary")
        self.close_button.clicked.connect(self.accept)
        row.addWidget(self.close_button)
        root.addLayout(row)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._refresh()
        if self._left:
            self._timer.start()

    @property
    def can_close(self) -> bool:
        return self._left <= 0

    def hide_forever(self) -> bool:
        """Отметил ли человек «Больше не показывать»."""
        return self.dont_show.isChecked()

    def _tick(self) -> None:
        self._left -= 1
        if self._left <= 0:
            self._timer.stop()
        self._refresh()

    def _refresh(self) -> None:
        self.close_button.setEnabled(self.can_close)
        self.close_button.setText(
            "Закрыть" if self.can_close else f"Закрыть ({self._left})")

    # Esc и крестик до конца отсчёта игнорируются.
    def reject(self) -> None:
        if self.can_close:
            super().reject()

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.can_close:
            super().closeEvent(event)
        else:
            event.ignore()
