"""Полоса-счётчик: сколько осталось обращений к ИИ и насколько велик текст.

Цвет подсказывает состояние без чтения цифр: синий — всё в порядке, жёлтый — запас
кончается, красный — предел исчерпан или превышен. Заливка плавно доезжает до нового
значения, а не прыгает, — так видно, что именно изменилось после обращения.
"""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ...core import ai
from ..theme import Palette

# С какой доли заполнения полоса желтеет и краснеет.
WARN_AT = 0.7
DANGER_AT = 0.9

_TONES = {
    "ok": Palette.PRIMARY,
    "warn": Palette.WARNING,
    "danger": Palette.DANGER,
    "muted": Palette.BORDER_STRONG,
}


def tone_for(ratio: float) -> str:
    if ratio >= DANGER_AT:
        return "danger"
    return "warn" if ratio >= WARN_AT else "ok"


def group(number: int) -> str:
    """12 345 — с пробелом между разрядами, как принято в тексте для людей."""
    return f"{number:,}".replace(",", " ")


class _Track(QWidget):
    """Подложка и заливка. Сама рисует и сама анимирует доезд заливки."""

    def __init__(self, height: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._shown = 0.0
        self._color = QColor(_TONES["muted"])
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(450)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._step)

    @property
    def shown(self) -> float:
        return self._shown

    def set_target(self, ratio: float, tone: str) -> None:
        self._color = QColor(_TONES[tone])
        ratio = min(max(ratio, 0.0), 1.0)
        self._animation.stop()
        self._animation.setStartValue(self._shown)
        self._animation.setEndValue(ratio)
        # Пока окна не видно, анимировать нечего — сразу ставим значение: так
        # проще и тестам не нужно ждать цикла событий.
        if self.isVisible():
            self._animation.start()
        else:
            self._shown = ratio
            self.update()

    def _step(self, value) -> None:
        self._shown = float(value)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        radius = self.height() / 2
        rect = QRectF(self.rect())
        painter.setBrush(QColor(Palette.BORDER))
        painter.drawRoundedRect(rect, radius, radius)
        if self._shown > 0:
            # Минимальная ширина — высота полосы: иначе малое значение
            # превращается в невидимую точку.
            width = max(rect.width() * self._shown, self.height())
            painter.setBrush(self._color)
            painter.drawRoundedRect(QRectF(0, 0, min(width, rect.width()), rect.height()),
                                    radius, radius)


class UsageMeter(QWidget):
    """Заголовок, значение справа, полоса и пояснение под ней.

    `compact` — одна строка для подвала: название, короткая полоса, значение.
    Пояснение тогда уходит в подсказку.
    """

    def __init__(self, title: str, parent: QWidget | None = None, *,
                 compact: bool = False) -> None:
        super().__init__(parent)
        self._compact = compact
        self.title = QLabel(title, self)
        self.title.setStyleSheet(f"color: {Palette.TEXT_MUTED}; font-size: 12px;")
        self.value = QLabel("", self)
        self.note = QLabel("", self)
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color: {Palette.TEXT_FAINT}; font-size: 11px;")
        self.track = _Track(6 if compact else 8, self)
        self.tone = "muted"

        if compact:
            row = QHBoxLayout(self)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            row.addWidget(self.title)
            self.track.setFixedWidth(110)
            row.addWidget(self.track)
            row.addWidget(self.value)
            self.note.hide()
        else:
            column = QVBoxLayout(self)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(5)
            head = QHBoxLayout()
            head.addWidget(self.title)
            head.addStretch(1)
            head.addWidget(self.value)
            column.addLayout(head)
            column.addWidget(self.track)
            column.addWidget(self.note)
        self.show_message("Загрузка…")

    # --- что показывать -------------------------------------------------------

    def show_progress(self, ratio: float, value: str, note: str = "",
                      tone: str | None = None) -> None:
        self.tone = tone or tone_for(ratio)
        self._paint(ratio, value, note)

    def show_message(self, text: str, note: str = "") -> None:
        """Вместо цифр — слово: нет входа, ИИ закрыт или не настроен."""
        self.tone = "muted"
        self._paint(0.0, text, note)

    def show_usage(self, usage: ai.Usage) -> None:
        """Остаток обращений на сегодня."""
        if not usage.configured:
            self.show_message("не настроен", "ИИ на сервере не настроен — сообщите администратору.")
            return
        if usage.closed:
            self.show_message("закрыт", "Администратор закрыл вам ИИ-правку. "
                                         "Если она нужна, попросите открыть.")
            return
        again = ai.reset_in(usage.reset_seconds)
        if usage.remaining == 0:
            note = f"Лимит на сегодня исчерпан: {usage.used} из {usage.limit}. Обнулится {again}."
        else:
            note = (f"Использовано {usage.used} из {usage.limit}. "
                    f"Счётчик обнулится {again} (полночь по Москве). "
                    "Предел задаёт администратор.")
        value = (f"{usage.used} из {usage.limit}" if self._compact
                 else f"осталось {usage.remaining} из {usage.limit}")
        self.show_progress(usage.ratio, value, note)

    def show_size(self, chars: int, maximum: int, what: str = "") -> None:
        """Размер текста относительно того, что принимает один запрос."""
        ratio = chars / maximum if maximum else 1.0
        value = f"{group(chars)} из {group(maximum)} знаков"
        if chars > maximum:
            note = (f"{what}Текст длиннее предела на {group(chars - maximum)} знаков: "
                    "выделите часть, которую нужно поправить.")
            self.show_progress(1.0, value, note, tone="danger")
        else:
            self.show_progress(ratio, value, f"{what}Помещается в один запрос.")

    # --- вид ------------------------------------------------------------------

    def _paint(self, ratio: float, value: str, note: str) -> None:
        self.value.setText(value)
        color = _TONES["danger"] if self.tone == "danger" else (
            _TONES["warn"] if self.tone == "warn" else Palette.TEXT)
        self.value.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: 600;")
        self.note.setText(note)
        self.note.setVisible(bool(note) and not self._compact)
        self.setToolTip(note)
        self.track.set_target(ratio, self.tone)
