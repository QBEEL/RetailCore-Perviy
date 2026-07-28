"""Список исключений: ручные привязки, которые применяются при каждом переносе."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.order import AliasBook
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle


class AliasesDialog(QDialog):
    """Просмотр и удаление исключений. Книга правится на месте."""

    def __init__(self, aliases: AliasBook, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.aliases = aliases
        self.setWindowTitle("Исключения переноса")
        self.resize(760, 440)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle("Сохранённые привязки", self))
        root.addWidget(Hint(
            "Позиция 1С слева, строка бланка справа. Привязка ищется по артикулу, "
            "штрихкоду и названию, поэтому переживает смену номеров строк в новом "
            "бланке. Исключение применяется раньше автоматического подбора.", self))

        self.list = QListWidget(self)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setAlternatingRowColors(True)
        root.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        remove = QPushButton("Удалить выбранные", self)
        remove.setObjectName("Danger")
        remove.clicked.connect(self._remove)
        buttons.addWidget(remove)

        clear = QPushButton("Удалить все", self)
        clear.clicked.connect(self._clear)
        buttons.addWidget(clear)
        buttons.addStretch(1)

        close = QPushButton("Закрыть", self)
        close.setObjectName("Primary")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        root.addLayout(buttons)

        self._fill()

    def _fill(self) -> None:
        self.list.clear()
        for alias in self.aliases.items:
            item = QListWidgetItem(alias.title)
            item.setToolTip(
                f"1С: {alias.source_name}\nартикул {alias.source_article} · {alias.source_ean}\n\n"
                f"Бланк: {alias.target_name}\nартикул {alias.target_article} · {alias.target_ean}")
            item.setData(Qt.ItemDataRole.UserRole, alias)
            self.list.addItem(item)
        if not self.aliases.items:
            empty = QListWidgetItem("Исключений нет")
            empty.setForeground(QColor(Palette.TEXT_MUTED))
            self.list.addItem(empty)

    def _remove(self) -> None:
        for item in self.list.selectedItems():
            if alias := item.data(Qt.ItemDataRole.UserRole):
                self.aliases.forget(alias)
        self._fill()

    def _clear(self) -> None:
        self.aliases.clear()
        self._fill()
