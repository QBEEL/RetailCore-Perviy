"""Список дополнительных каталогов: архивные прайсы и прайсы других каналов."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import icons
from .file_picker import EXCEL_FILTER

_ROW_HEIGHT = 30
_MAX_VISIBLE_ROWS = 4


class CatalogList(QFrame):
    """Дополнительные каталоги ищутся после основного, в порядке списка."""

    changed = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._paths: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(7)
        title = QLabel("Дополнительные каталоги", self)
        title.setStyleSheet("font-weight: 600;")
        header.addWidget(title)
        self._hint = QLabel("необязательно", self)
        self._hint.setObjectName("Hint")
        header.addWidget(self._hint, 1)

        add = QPushButton("Добавить", self)
        add.setIcon(icons.icon("open"))
        add.setToolTip("Архивные прайсы или прайсы других каналов — в них ищутся "
                       "позиции, которых нет в основном каталоге")
        add.clicked.connect(self.browse)
        header.addWidget(add)
        root.addLayout(header)

        self.list = QListWidget(self)
        self.list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.setVisible(False)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        root.addWidget(self.list)

    @property
    def paths(self) -> list[str]:
        return list(self._paths)

    def browse(self) -> None:
        start = os.path.dirname(self._paths[-1]) if self._paths else ""
        paths, _ = QFileDialog.getOpenFileNames(self, "Выберите дополнительные каталоги", start, EXCEL_FILTER)
        if paths:
            self.add(paths)

    def add(self, paths: list[str]) -> None:
        added = [p for p in paths if p not in self._paths]
        if not added:
            return
        self._paths.extend(added)
        self._rebuild()
        self.changed.emit(self.paths)

    def set_paths(self, paths: list[str]) -> None:
        self._paths = [p for p in paths if os.path.exists(p)]
        self._rebuild()

    def set_status(self, path: str, text: str) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == path:
                item.setText(f"{os.path.basename(path)}   —   {text}")
                item.setToolTip(f"{path}\n{text}")
                return

    def remove_selected(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self._paths):
            del self._paths[row]
            self._rebuild()
            self.changed.emit(self.paths)

    def _rebuild(self) -> None:
        self.list.clear()
        for path in self._paths:
            item = QListWidgetItem(f"{os.path.basename(path)}   —   ожидает загрузки")
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setIcon(icons.icon("file"))
            item.setToolTip(path)
            self.list.addItem(item)
        # Высота задаётся явно: иначе список не резервирует место и наезжает
        # на следующий блок карточки.
        visible_rows = min(len(self._paths), _MAX_VISIBLE_ROWS)
        self.list.setFixedHeight(visible_rows * _ROW_HEIGHT + 10 if visible_rows else 0)
        self.list.setVisible(bool(self._paths))
        self.list.updateGeometry()
        self._hint.setText(
            f"{len(self._paths)} шт. · ищутся после основного" if self._paths else "необязательно")

    def _menu(self, position) -> None:
        if not self.list.itemAt(position):
            return
        menu = QMenu(self)
        menu.addAction("Убрать из списка", self.remove_selected)
        menu.exec(self.list.viewport().mapToGlobal(position))

    def dragEnterEvent(self, event) -> None:
        if self._files_from(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        if paths := self._files_from(event):
            self.add(paths)
            event.acceptProposedAction()

    @staticmethod
    def _files_from(event) -> list[str]:
        data = event.mimeData()
        if not data.hasUrls():
            return []
        return [
            url.toLocalFile() for url in data.urls()
            if url.toLocalFile().lower().endswith((".xlsx", ".xlsm", ".xls"))
        ]
