"""Таблица: сортировка, фильтр, подсветка совпадений, копирование, скрытие колонок."""
from __future__ import annotations

from typing import Any, Callable, Sequence

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QGuiApplication, QKeySequence, QPainter, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QWidget,
)

from ..theme import Metrics, Palette
from ...core.search import highlight_terms

ROLE_SORT = Qt.ItemDataRole.UserRole + 1
ROLE_TERMS = Qt.ItemDataRole.UserRole + 2
ROLE_PAYLOAD = Qt.ItemDataRole.UserRole + 3
ROLE_STATUS = Qt.ItemDataRole.UserRole + 4


class Column:
    """Описание колонки таблицы: как достать значение и как его отсортировать."""

    def __init__(
        self,
        title: str,
        getter: Callable[[Any], Any],
        width: int = 140,
        sort_key: Callable[[Any], Any] | None = None,
        align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        color: Callable[[Any], QColor | None] | None = None,
        highlight: bool = False,
    ) -> None:
        self.title = title
        self.getter = getter
        self.width = width
        self.sort_key = sort_key or getter
        self.align = align
        self.color = color
        self.highlight = highlight


class ObjectTableModel(QAbstractTableModel):
    """Модель поверх произвольных объектов: колонки задаются функциями доступа."""

    def __init__(self, columns: Sequence[Column], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._columns = list(columns)
        self._items: list[Any] = []
        self._terms: list[str] = []
        self._status: Callable[[Any], str] | None = None

    def set_items(self, items: Sequence[Any]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def set_terms(self, terms: Sequence[str]) -> None:
        self._terms = [t for t in terms if t]
        if self._items:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._items) - 1, len(self._columns) - 1),
                [ROLE_TERMS],
            )

    def set_status_provider(self, provider: Callable[[Any], str] | None) -> None:
        self._status = provider

    def item_at(self, row: int) -> Any:
        return self._items[row] if 0 <= row < len(self._items) else None

    def refresh_item(self, item: Any) -> None:
        """Перерисовывает строку после изменения объекта вне модели."""
        try:
            row = self._items.index(item)
        except ValueError:
            return
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(self._columns) - 1))

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        item = self._items[index.row()]
        column = self._columns[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            value = column.getter(item)
            return "" if value is None else str(value)
        if role == Qt.ItemDataRole.ToolTipRole:
            value = column.getter(item)
            return None if value is None else str(value)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(column.align)
        if role == Qt.ItemDataRole.ForegroundRole and column.color:
            return column.color(item)
        if role == ROLE_SORT:
            return column.sort_key(item)
        if role == ROLE_TERMS:
            return self._terms if column.highlight else []
        if role == ROLE_PAYLOAD:
            return item
        if role == ROLE_STATUS:
            return self._status(item) if self._status else ""
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._columns[section].title
        return section + 1

    @property
    def columns(self) -> Sequence[Column]:
        return self._columns


class FilterProxy(QSortFilterProxyModel):
    """Фильтр по тексту и по статусу одновременно."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSortRole(ROLE_SORT)
        self._text = ""
        self._status = ""

    def set_text(self, text: str) -> None:
        self._text = text.casefold().replace("ё", "е").strip()
        self.invalidateFilter()

    def set_status(self, status: str) -> None:
        self._status = status
        self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if self._status:
            if model.data(model.index(row, 0, parent), ROLE_STATUS) != self._status:
                return False
        if not self._text:
            return True
        for column in range(model.columnCount()):
            value = model.data(model.index(row, column, parent), Qt.ItemDataRole.DisplayRole)
            if value and self._text in str(value).casefold().replace("ё", "е"):
                return True
        return False

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        a = self.sourceModel().data(left, ROLE_SORT)
        b = self.sourceModel().data(right, ROLE_SORT)
        if a is None:
            return True
        if b is None:
            return False
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


class HighlightDelegate(QStyledItemDelegate):
    """Подсвечивает найденные фрагменты прямо в ячейке."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        terms = index.data(ROLE_TERMS)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        spans = highlight_terms(text, terms) if terms else []
        if not spans:
            super().paint(painter, option, index)
            return

        self.initStyleOption(option, index)
        style = option.widget.style() if option.widget else None
        option.text = ""
        if style:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)

        document = QTextDocument()
        document.setDefaultFont(option.font)
        document.setDocumentMargin(0)
        document.setHtml(_markup(text, spans))
        # Без переноса: строка таблицы имеет фиксированную высоту, и второй ряд
        # текста выходил бы за её пределы.
        document.setTextWidth(-1)

        painter.save()
        painter.setClipRect(option.rect)
        painter.translate(
            option.rect.left() + 8,
            option.rect.top() + (option.rect.height() - document.size().height()) / 2,
        )
        document.drawContents(painter)
        painter.restore()


def _markup(text: str, spans: Sequence[tuple[int, int]]) -> str:
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(_escape(text[cursor:start]))
        parts.append(f'<span style="background:{Palette.HIGHLIGHT}; border-radius:2px;">{_escape(text[start:end])}</span>')
        cursor = end
    parts.append(_escape(text[cursor:]))
    return "".join(parts)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class DataTable(QTableView):
    """Таблица с сортировкой, подсветкой, копированием и меню колонок."""

    item_activated = Signal(object)

    def __init__(self, columns: Sequence[Column], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model_ = ObjectTableModel(columns, self)
        self.proxy = FilterProxy(self)
        self.proxy.setSourceModel(self.model_)
        self.setModel(self.proxy)

        self.setItemDelegate(HighlightDelegate(self))
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSortingEnabled(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setMouseTracking(True)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(Metrics.ROW_HEIGHT)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

        header = self.horizontalHeader()
        # Без явного сброса Qt включает сортировку по первому столбцу и ломает
        # порядок, заданный движком поиска (по убыванию оценки совпадения).
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        header.setSortIndicatorShown(True)
        header.setSectionsMovable(True)
        header.setStretchLastSection(True)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._column_menu)
        for index, column in enumerate(columns):
            header.resizeSection(index, column.width)
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)

        self.doubleClicked.connect(self._emit_activated)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._row_menu)

        copy = QAction("Копировать", self)
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        copy.triggered.connect(self.copy_selection)
        self.addAction(copy)

    def set_items(self, items: Sequence[Any]) -> None:
        self.model_.set_items(items)

    def current_item(self) -> Any:
        index = self.currentIndex()
        return None if not index.isValid() else self.proxy.mapToSource(index).data(ROLE_PAYLOAD)

    def selected_items(self) -> list[Any]:
        rows = {i.row() for i in self.selectionModel().selectedIndexes()}
        return [self.proxy.index(row, 0).data(ROLE_PAYLOAD) for row in sorted(rows)]

    def select_item(self, item: Any) -> None:
        for row in range(self.proxy.rowCount()):
            if self.proxy.index(row, 0).data(ROLE_PAYLOAD) is item:
                self.selectRow(row)
                self.scrollTo(self.proxy.index(row, 0), QAbstractItemView.ScrollHint.PositionAtCenter)
                return

    def copy_selection(self) -> int:
        """Копирует выделение в буфер как TSV — вставляется прямо в Excel."""
        indexes = self.selectionModel().selectedIndexes()
        if not indexes:
            return 0
        rows = sorted({i.row() for i in indexes})
        columns = sorted({i.column() for i in indexes})
        lines = [
            "\t".join(str(self.proxy.index(row, column).data() or "") for column in columns)
            for row in rows
        ]
        QGuiApplication.clipboard().setText("\n".join(lines))
        return len(rows)

    def _emit_activated(self, index: QModelIndex) -> None:
        if item := self.proxy.mapToSource(index).data(ROLE_PAYLOAD):
            self.item_activated.emit(item)

    def _column_menu(self, position) -> None:
        menu = QMenu(self)
        menu.addAction("Подогнать по содержимому", self.resizeColumnsToContents)
        menu.addSeparator()
        for index, column in enumerate(self.model_.columns):
            action = menu.addAction(column.title)
            action.setCheckable(True)
            action.setChecked(not self.isColumnHidden(index))
            action.toggled.connect(lambda visible, i=index: self.setColumnHidden(i, not visible))
        menu.exec(self.horizontalHeader().mapToGlobal(position))

    def _row_menu(self, position) -> None:
        index = self.indexAt(position)
        if not index.isValid():
            return
        menu = QMenu(self)
        menu.addAction("Копировать\tCtrl+C", self.copy_selection)
        menu.addAction("Открыть карточку", lambda: self._emit_activated(index))
        menu.exec(self.viewport().mapToGlobal(position))
