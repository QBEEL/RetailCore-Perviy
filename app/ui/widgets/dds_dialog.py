"""Окно «Цвет и метка» для статей ДДС.

Задаётся сразу нескольким статьям: «Маркетинг (ГПН)» и «Маркетинг (Адвент)»
получают один цвет и одну подпись за один раз, а не по очереди.
"""
from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ...core.payments import dds
from ..theme import Metrics
from . import marks
from .common import Hint
from .inputs import SelectBox

NOTE_LIMIT = 60


class DdsMarkDialog(QDialog):
    """Выбор цвета и метки для выбранных статей."""

    def __init__(self, items: list[dds.DdsItem], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Цвет и метка статьи")
        self.setMinimumWidth(480)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 4, Metrics.PAD, Metrics.PAD + 4, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        shown = ", ".join(f"«{item.title}»" for item in items[:3])
        more = f" и ещё {len(items) - 3}" if len(items) > 3 else ""
        title = QLabel(f"Статьи: {shown}{more}", self)
        title.setWordWrap(True)
        root.addWidget(title)

        form = QFormLayout()
        form.setSpacing(9)
        self.color = SelectBox(self)
        self.color.addItem("Без цвета", "")
        for name, code in dds.COLORS:
            self.color.addItem(marks.dot_icon(QColor(code)), name, code)
        form.addRow("Цвет", self.color)

        self.note = QLineEdit(self)
        self.note.setMaxLength(NOTE_LIMIT)
        self.note.setPlaceholderText("Например, «Маркетинг» — без метки виден только кружок")
        form.addRow("Метка", self.note)
        root.addLayout(form)

        root.addWidget(Hint(
            "Оплаты с этими статьями получат цветной кружок и подсветку строки в "
            "таблице и календаре, а метка появится рядом с оплатой. Если выбранные "
            "статьи уже окрашены по-разному, будет задано то, что выбрано здесь."))

        self.error = Hint("", self)
        root.addWidget(self.error)

        buttons = QDialogButtonBox(self)
        save = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        save.setText("Сохранить")
        save.setObjectName("Primary")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # Если у всех выбранных одно и то же — показываем это, иначе пусто.
        colors = {item.color.upper() for item in items}
        notes = {item.note for item in items}
        if len(colors) == 1:
            self.color.setCurrentIndex(max(self.color.findData(colors.pop()), 0))
        if len(notes) == 1:
            self.note.setText(notes.pop())

    def result_mark(self) -> tuple[str, str]:
        """Цвет «#RRGGBB» (или пусто) и метка без лишних пробелов."""
        return self.color.currentData() or "", " ".join(self.note.text().split())
