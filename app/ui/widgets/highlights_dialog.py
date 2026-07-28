"""Позиции прайса с пометками поставщика: НОВИНКА, ХИТ, LIMITED."""
from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.order import TargetRow
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle
from .inputs import pass_wheel

_COLUMNS = ("Пометка", "Артикул", "Наименование", "Заказ, шт.")
_QTY = 3


class _QuantityEditor(QSpinBox):
    """Редактор количества. Существует только между щелчком и подтверждением.

    Пока щелчка не было, в таблице нет ни одного виджета, способного принять
    фокус или ввод, — наведению мыши просто нечего активировать.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setRange(0, 100000)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # Общий отступ полей рассчитан на форму, в ячейке он съедает высоту.
        self.setStyleSheet("padding: 1px 6px;")

    def wheelEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        # Колесо листает список, а не крутит значение.
        pass_wheel(self, event)


class _Quantity(QStyledItemDelegate):
    """Колонка количества: редактор по щелчку и заливка редактируемой строки."""

    def __init__(self, dialog: "HighlightsDialog") -> None:
        super().__init__(dialog)
        self._dialog = dialog
        self._tint = QColor(Palette.PRIMARY)
        self._tint.setAlpha(28)
        self.row: int | None = None  # строка открытого редактора

    def paint(self, painter, option, index) -> None:  # type: ignore[no-untyped-def]
        # Рамку «текущей ячейки» таблица рисует сама — снимаем: активную
        # строку показывает заливка, вторая рамка только путает.
        option.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(painter, option, index)
        if index.row() == self.row:
            painter.fillRect(option.rect, self._tint)

    def createEditor(self, parent, option, index):  # type: ignore[no-untyped-def]
        editor = _QuantityEditor(parent)
        editor.setProperty("row", index.row())
        self.row = index.row()
        self._dialog.table.viewport().update()
        return editor

    def setEditorData(self, editor, index) -> None:  # type: ignore[no-untyped-def]
        editor.setValue(int(index.data(Qt.ItemDataRole.EditRole) or 0))
        # Редактор появляется только после щелчка, поэтому выделение здесь и
        # означает «выделять по клику»: набранное число заменяет прежнее.
        editor.selectAll()

    def setModelData(self, editor, model, index) -> None:  # type: ignore[no-untyped-def]
        editor.interpretText()
        model.setData(index, editor.value(), Qt.ItemDataRole.EditRole)

    def destroyEditor(self, editor, index) -> None:  # type: ignore[no-untyped-def]
        super().destroyEditor(editor, index)
        self.row = None
        self._dialog.table.viewport().update()


class HighlightsDialog(QDialog):
    """Выбор новинок и хитов: пользователь ставит количество, строки уходят в заказ."""

    def __init__(self, entries: Sequence[TargetRow], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entries = list(entries)
        self.setWindowTitle("Новинки и хиты в прайсе")
        self.resize(920, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle(f"Отмечено поставщиком: {len(self.entries)} позиций", self))
        root.addWidget(Hint(
            "Этих позиций нет в вашем заказе. Щёлкните по количеству, введите число — "
            "Enter подтверждает и переходит к следующей строке. Строки с количеством "
            "добавятся в заказ и запишутся в бланк вместе с остальными.", self))

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Фильтр по названию, артикулу или пометке…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        root.addWidget(self.search)

        self.table = QTableWidget(len(self.entries), len(_COLUMNS), self)
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        # Строка должна вмещать редактор целиком, иначе число в нём обрезается.
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setAlternatingRowColors(True)
        # Редактор открывает только явный щелчок по ячейке количества (ниже,
        # через cellClicked): ни наведение, ни клавиши сами ввод не начинают.
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Подсветка строки означает ровно одно: сюда идёт ввод. Поэтому обычное
        # выделение строк отключено, заливку рисует делегат по открытому редактору.
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setStyleSheet("QTableWidget::item:hover { background: transparent; }")
        self._delegate = _Quantity(self)
        self.table.setItemDelegate(self._delegate)
        self._fill()
        self.table.cellClicked.connect(self._cell_clicked)
        self.table.itemChanged.connect(self._changed)
        # Лямбда обязательна: у closeEditor аргумент hint со значением по
        # умолчанию, и прямое подключение метода PySide не разрешает.
        self._delegate.closeEditor.connect(
            lambda editor, hint=QAbstractItemDelegate.EndEditHint.NoHint:
            self._editor_closed(editor, hint))
        self.table.verticalScrollBar().valueChanged.connect(self._release_hidden)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        self.total = Hint("", self)
        buttons.addWidget(self.total, 1)

        cancel = QPushButton("Отмена", self)
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)

        add = QPushButton("Добавить в заказ", self)
        add.setObjectName("Primary")
        # Иначе Enter при вводе количества закрывал бы окно на первой же строке.
        add.setAutoDefault(False)
        add.clicked.connect(self.accept)
        buttons.addWidget(add)
        root.addLayout(buttons)
        self._update_total()

    def _fill(self) -> None:
        for row, entry in enumerate(self.entries):
            marks = QTableWidgetItem(" / ".join(entry.marks))
            marks.setForeground(QColor(Palette.PRIMARY))
            self.table.setItem(row, 0, marks)
            self.table.setItem(row, 1, QTableWidgetItem(entry.article))
            title = QTableWidgetItem(entry.title)
            title.setToolTip(f"{entry.title}\nстрока бланка {entry.row}")
            self.table.setItem(row, 2, title)
            for column in range(_QTY):
                self.table.item(row, column).setFlags(Qt.ItemFlag.ItemIsEnabled)

            quantity = QTableWidgetItem()
            quantity.setData(Qt.ItemDataRole.EditRole, 0)
            quantity.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            quantity.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, _QTY, quantity)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(1, 170)
        self.table.setColumnWidth(_QTY, 130)

    def _value(self, row: int) -> int:
        return int(self.table.item(row, _QTY).data(Qt.ItemDataRole.EditRole) or 0)

    def _cell_clicked(self, row: int, column: int) -> None:
        """Единственный путь к вводу: щелчок по ячейке количества."""
        if column == _QTY:
            self.table.editItem(self.table.item(row, _QTY))

    def _changed(self, item: QTableWidgetItem) -> None:
        if item.column() == _QTY:
            self._update_total()

    def _editor_closed(self, editor: QWidget, hint) -> None:  # type: ignore[no-untyped-def]
        """Enter подтверждает число и открывает редактор в следующей строке."""
        if hint != QAbstractItemDelegate.EndEditHint.SubmitModelCache:
            return
        row = int(editor.property("row"))
        # Следующий редактор нельзя открывать прямо из сигнала closeEditor:
        # таблица ещё удаляет прежний, и немедленный editItem роняет процесс.
        QTimer.singleShot(0, self, lambda: self._edit_next(row))

    def _edit_next(self, row: int) -> None:
        for next_row in range(row + 1, len(self.entries)):
            if not self.table.isRowHidden(next_row):
                item = self.table.item(next_row, _QTY)
                self.table.scrollToItem(item)
                self.table.editItem(item)
                return

    def _release_hidden(self) -> None:
        """Редактор, уехавший при прокрутке за край, подтверждает число и закрывается.

        Иначе набор продолжался бы в невидимой строке: на экране одна
        номенклатура, количество уходит в другую. Пока редактор на виду,
        прокрутка на пару строк вводу не мешает.
        """
        focused = self.focusWidget()
        editor = focused if isinstance(focused, _QuantityEditor) else (
            focused.parentWidget() if focused is not None else None)
        if not isinstance(editor, _QuantityEditor):
            return
        item = self.table.item(int(editor.property("row")), 2)
        if not self.table.viewport().rect().intersects(self.table.visualItemRect(item)):
            self.table.setFocus()

    def _filter(self, text: str) -> None:
        query = text.casefold().replace("ё", "е").strip()
        for row, entry in enumerate(self.entries):
            blob = f"{' '.join(entry.marks)} {entry.article} {entry.title}".casefold().replace("ё", "е")
            # Строка со введённым количеством не прячется: иначе фильтр молча
            # выбросил бы уже сделанный выбор.
            self.table.setRowHidden(row, bool(query) and query not in blob
                                    and not self._value(row))

    def _update_total(self) -> None:
        chosen = self.chosen()
        units = sum(quantity for _, quantity in chosen)
        self.total.setText(f"Выбрано позиций: {len(chosen)} · штук: {units:g}" if chosen
                           else "Количество не указано")

    def chosen(self) -> list[tuple[TargetRow, int]]:
        return [(entry, self._value(row))
                for row, entry in enumerate(self.entries) if self._value(row) > 0]
