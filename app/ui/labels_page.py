"""Вкладка «Этикетки»: ценники для обклейки товара из файла номенклатуры."""
from __future__ import annotations

import os
from dataclasses import replace
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core import price_labels, workbook
from ..core import price_label_spec as spec_store
from ..core.price_labels import PriceLabel
from ..core.settings import AppSettings
from . import icons, label_print, price_label_print
from .tasks import run_task
from .theme import Metrics, Palette
from .widgets.common import Card, Hint, MetricTile, SectionTitle, Subtitle, Title, fade_in
from .widgets.file_picker import FilePicker
from .widgets.inputs import SelectBox
from .widgets.label_template_dialog import DiscountDialog, TemplateDialog, preview_zoom
from .widgets.table import Column, DataTable
from .widgets.toast import ToastKind

_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_FIT_PROBLEM = "штрихкод слишком длинный"


class LabelsPage(QWidget):
    """Из файла номенклатуры — этикетки на термопринтер: сколько указано, столько и печатается."""

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, ToastKind], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self.labels: list[PriceLabel] = []
        # Убранные из списка позиции — чтобы их можно было вернуть, не открывая файл заново.
        self._removed: list[PriceLabel] = []
        self.print_settings = spec_store.load()
        self.spec = self.print_settings.select(self.print_settings.template)
        self._build()

    # --- интерфейс ------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Title("Этикетки", self))
        root.addWidget(Subtitle(
            "Ценники для обклейки на термопринтер. Файл Excel: "
            "Номенклатура, Характеристика, Штрихкод, Цена, Количество — "
            "сколько указано в «Количестве», столько этикеток и напечатается.", self))

        root.addWidget(self._file_card())
        root.addWidget(self._metrics_row())
        root.addWidget(self._workspace(), 1)

    def _file_card(self) -> Card:
        card = Card(self)
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.picker = FilePicker("Номенклатура", "не выбрана", card,
                                 file_filter="Excel (*.xlsx *.xlsm *.xls);;Все файлы (*.*)")
        self.picker.file_selected.connect(self._on_file)
        self.picker.sheet_changed.connect(lambda name: self._load(self.picker.path, name))
        if self.print_settings.last_file:
            self.picker.set_recent([self.print_settings.last_file])
        card.body().addWidget(self.picker)
        return card

    def _metrics_row(self) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Metrics.GAP)
        self.tile_items = MetricTile("Позиций", Palette.PRIMARY, row)
        self.tile_copies = MetricTile("Этикеток к печати", Palette.SUCCESS, row)
        self.tile_skipped = MetricTile("Пропущено", Palette.DANGER, row)
        for tile in (self.tile_items, self.tile_copies, self.tile_skipped):
            layout.addWidget(tile)
        return row

    def _workspace(self) -> QSplitter:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._table_card())
        splitter.addWidget(self._print_card())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([820, 380])
        return splitter

    def _table_card(self) -> Card:
        card = Card(self, padding=Metrics.PAD - 4)
        body = card.body()
        body.setSpacing(Metrics.GAP - 4)

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Позиции", card))
        self.search = QLineEdit(card)
        self.search.setPlaceholderText("Поиск по позициям…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda text: self.table.proxy.set_text(text))
        header.addWidget(self.search, 1)
        self.remove_button = QPushButton("Убрать", card)
        self.remove_button.setIcon(icons.icon("trash"))
        self.remove_button.setToolTip(
            "Убрать выбранные позиции из списка печати (Delete). Файл Excel не меняется.")
        self.remove_button.clicked.connect(self.remove_selected)
        header.addWidget(self.remove_button)
        self.restore_button = QPushButton("Вернуть убранные", card)
        self.restore_button.setIcon(icons.icon("reset"))
        self.restore_button.setToolTip("Вернуть в список все позиции, убранные из него")
        self.restore_button.clicked.connect(self.restore_removed)
        self.restore_button.setVisible(False)
        header.addWidget(self.restore_button)
        body.addLayout(header)

        self.table = DataTable([
            Column("Строка", lambda x: x.row, 56, align=_RIGHT),
            Column("Номенклатура", lambda x: x.name, 205, highlight=True),
            Column("Характеристика", lambda x: x.variant, 135, highlight=True),
            Column("Штрихкод", lambda x: x.barcode, 112),
            Column("Цена", lambda x: price_labels.format_price(x.price), 58,
                   sort_key=lambda x: x.price or 0, align=_RIGHT),
            Column("Со скидкой", lambda x: price_labels.format_price(x.price2), 88,
                   sort_key=lambda x: x.price2 or 0, align=_RIGHT),
            Column("Кол-во", lambda x: x.copies, 62, align=_RIGHT),
            Column("Состояние", lambda x: x.problem or "", 150, color=self._problem_color),
        ], card)
        self._sync_columns()
        self.table.item_activated.connect(self._edit_copies)
        self.table.selectionModel().selectionChanged.connect(self._refresh_preview)
        delete = QAction("Убрать из списка", self.table)
        delete.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        delete.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        delete.triggered.connect(self.remove_selected)
        self.table.addAction(delete)
        body.addWidget(self.table, 1)
        body.addWidget(Hint(
            "Двойной щелчок по строке — изменить количество этикеток. Delete — убрать "
            "выбранные позиции из списка.", card))
        return card

    @staticmethod
    def _problem_color(item: PriceLabel) -> QColor | None:
        return QColor(Palette.DANGER) if item.problem else None

    def _print_card(self) -> Card:
        card = Card(self, padding=Metrics.PAD - 4)
        body = card.body()
        body.setSpacing(Metrics.GAP - 4)
        body.addWidget(SectionTitle("Печать", card))

        body.addWidget(self._caption("Шаблон этикетки"))
        self.template_box = SelectBox(card)
        self.template_box.setToolTip(
            "Размер этикетки и число цен. Шаблон можно настроить под свой принтер "
            "или сделать на его основе свой.")
        self._fill_templates()
        self.template_box.currentIndexChanged.connect(self._template_picked)
        body.addWidget(self.template_box)
        tools = QHBoxLayout()
        tools.setSpacing(6)
        self.configure_button = QPushButton("Настроить…", card)
        self.configure_button.clicked.connect(self.configure_template)
        self.copy_button = QPushButton("Копия…", card)
        self.copy_button.setToolTip("Новый шаблон на основе выбранного")
        self.copy_button.clicked.connect(self.copy_template)
        self.delete_button = QPushButton("Удалить", card)
        self.delete_button.setToolTip("Удаляются только свои шаблоны")
        self.delete_button.clicked.connect(self.delete_template)
        for button in (self.configure_button, self.copy_button, self.delete_button):
            tools.addWidget(button)
        body.addLayout(tools)
        discount_row = QHBoxLayout()
        discount_row.setSpacing(8)
        self.discount_label = QLabel("", card)
        discount_row.addWidget(self.discount_label, 1)
        self.discount_button = QPushButton("Скидка…", card)
        self.discount_button.clicked.connect(self.change_discount)
        discount_row.addWidget(self.discount_button)
        body.addLayout(discount_row)
        self._sync_template_controls()

        self.preview_label = QLabel(card)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(self._preview_height())
        self.preview_label.setStyleSheet(
            f"background: {Palette.SURFACE_ALT}; border-radius: {Metrics.RADIUS_SM}px;")
        body.addWidget(self.preview_label)
        self.preview_hint = Hint("", card)
        self.preview_hint.setWordWrap(True)
        body.addWidget(self.preview_hint)

        body.addWidget(self._caption("Между номенклатурами"))
        self.gap_box = SelectBox(card)
        self.gap_box.addItem("Печатать подряд", spec_store.GAP_CONTINUOUS)
        self.gap_box.addItem("Ждать, пока оторвут этикетки", spec_store.GAP_TEAR)
        self.gap_box.addItem("Пустая этикетка между номенклатурами", spec_store.GAP_BLANK)
        self.gap_box.setCurrentIndex(max(0, self.gap_box.findData(self.print_settings.gap_mode)))
        self.gap_box.setToolTip(
            "Подряд — вся очередь одним заданием.\n"
            "Ждать — после каждой номенклатуры программа останавливается, пока вы не "
            "оторвёте напечатанное и не нажмёте «Печатать дальше».\n"
            "Пустая этикетка — после каждой номенклатуры, кроме последней, идёт чистая "
            "этикетка: по ней видно, где рвать.")
        self.gap_box.currentIndexChanged.connect(self._gap_changed)
        body.addWidget(self.gap_box)
        self.gap_hint = Hint("", card)
        self.gap_hint.setWordWrap(True)
        body.addWidget(self.gap_hint)
        self._show_gap_hint()

        body.addWidget(self._caption("Принтер"))
        self.printer_box = SelectBox(card)
        names = label_print.printers()
        self.printer_box.addItems(names)
        wanted = self.print_settings.printer or label_print.suggested_printer()
        if wanted in names:
            self.printer_box.setCurrentText(wanted)
        body.addWidget(self.printer_box)
        self.pdf_box = QCheckBox("Сохранить в PDF вместо печати", card)
        body.addWidget(self.pdf_box)

        shift = QHBoxLayout()
        shift.setSpacing(8)
        self.offset_x = self._spin(self.spec.offset_x, -5, 5, 0.5)
        self.offset_y = self._spin(self.spec.offset_y, -5, 5, 0.5)
        for caption, spin in (("Сдвиг вправо, мм", self.offset_x),
                              ("Сдвиг вниз, мм", self.offset_y)):
            column = QVBoxLayout()
            column.setSpacing(2)
            column.addWidget(self._caption(caption))
            column.addWidget(spin)
            shift.addLayout(column)
        body.addLayout(shift)
        body.addWidget(Hint("Если печать съезжает, подвиньте на долю миллиметра.", card))

        body.addStretch(1)

        self.print_button = QPushButton("Напечатать все", card)
        self.print_button.setObjectName("Primary")
        self.print_button.setIcon(icons.icon("run", Palette.TEXT_ON_PRIMARY))
        self.print_button.clicked.connect(lambda: self.print_labels(selected_only=False))
        body.addWidget(self.print_button)

        self.selected_button = QPushButton("Напечатать выбранные", card)
        self.selected_button.clicked.connect(lambda: self.print_labels(selected_only=True))
        body.addWidget(self.selected_button)

        self.test_button = QPushButton("Пробная этикетка", card)
        self.test_button.setToolTip(
            "Одна этикетка выбранной строки — проверить, что печать не съехала")
        self.test_button.clicked.connect(self.print_test)
        body.addWidget(self.test_button)

        self.status = Hint("Выберите файл", card)
        self.status.setWordWrap(True)
        body.addWidget(self.status)

        for spin in (self.offset_x, self.offset_y):
            spin.valueChanged.connect(self._spec_changed)
        self._sync_buttons()
        return card

    def _caption(self, text: str) -> QLabel:
        label = QLabel(text, self)
        label.setObjectName("Hint")
        return label

    def _spin(self, value: float, low: float, high: float, step: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setRange(low, high)
        spin.setDecimals(1)
        spin.setSingleStep(step)
        spin.setValue(value)
        return spin

    # --- файл -----------------------------------------------------------------

    def restore(self) -> None:
        """Открывает файл прошлого раза, если он на месте и пока ничего не открыто."""
        if self.labels or not (path := self.print_settings.last_file):
            return
        if os.path.exists(path):
            self.picker.set_path(path)

    def _on_file(self, path: str) -> None:
        try:
            sheets = workbook.list_sheets(path)
        except ValueError as error:
            self._fail(str(error))
            return
        self.picker.set_sheets(sheets, sheets[0] if sheets else "")
        self._load(path, sheets[0] if sheets else None)

    def _load(self, path: str, sheet: str | None) -> None:
        if not path:
            return
        self.picker.set_status("чтение файла…")
        run_task(price_labels.load, path, sheet or None,
                 on_result=lambda items, p=path: self._loaded(p, items),
                 on_error=self._fail)

    def _loaded(self, path: str, items: list[PriceLabel]) -> None:
        self.labels = items
        self._removed = []
        self._recheck_fit()
        self._apply_discount()
        self.table.set_items(items)
        self.picker.set_status(os.path.basename(path))
        self.print_settings.last_file = path
        self._save_settings()
        self._update_metrics()
        if items:
            self.table.selectRow(0)
        fade_in(self.table, 160)
        skipped = sum(1 for x in items if x.problem)
        self.status.setStyleSheet("")
        self.status.setText(
            f"Загружено позиций: {len(items)}."
            + (f" Не будут напечатаны из-за ошибок: {skipped}." if skipped else ""))
        self._sync_buttons()

    def _fail(self, message: str) -> None:
        self.picker.set_status("файл не прочитан", Palette.DANGER)
        self.status.setStyleSheet(f"color: {Palette.DANGER};")
        self.status.setText(message)
        self.notify(message, ToastKind.ERROR)

    # --- таблица --------------------------------------------------------------

    def _update_metrics(self) -> None:
        self.tile_items.set_value(len(self.labels))
        self.tile_copies.set_value(price_label_print.total_copies(self.labels))
        self.tile_skipped.set_value(sum(1 for x in self.labels if x.problem or not x.copies))

    def _edit_copies(self, item: PriceLabel) -> None:
        if item.problem:
            self.notify(f"Строка {item.row} не печатается: {item.problem}.", ToastKind.WARNING)
            return
        value, accepted = QInputDialog.getInt(
            self, "Количество этикеток", f"{item.title}\nСколько этикеток напечатать:",
            item.copies, 0, 10_000)
        if not accepted:
            return
        item.copies = value
        self.table.model_.refresh_item(item)
        self._update_metrics()
        self._sync_buttons()

    def remove_selected(self) -> None:
        """Убирает выбранные позиции из списка печати. Исходный файл не меняется."""
        chosen = self._selected()
        if not chosen:
            return
        gone = {id(item) for item in chosen}
        first_row = min(
            (index.row() for index in self.table.selectionModel().selectedRows()),
            default=0)
        self.labels = [item for item in self.labels if id(item) not in gone]
        self._removed.extend(chosen)
        self._after_list_change(select_row=first_row)
        self.status.setStyleSheet("")
        self.status.setText(
            f"Убрано из списка: {len(chosen)}. Файл Excel не изменён, убранное можно "
            "вернуть кнопкой «Вернуть убранные».")

    def restore_removed(self) -> None:
        if not self._removed:
            return
        count = len(self._removed)
        self.labels = sorted([*self.labels, *self._removed], key=lambda item: item.row)
        self._removed = []
        self._after_list_change()
        self.status.setStyleSheet("")
        self.status.setText(f"Возвращено в список: {count}.")

    def _after_list_change(self, select_row: int | None = None) -> None:
        self.table.set_items(self.labels)
        if select_row is not None and self.table.proxy.rowCount():
            self.table.selectRow(min(select_row, self.table.proxy.rowCount() - 1))
        self.restore_button.setVisible(bool(self._removed))
        self._update_metrics()
        self._refresh_preview()
        self._sync_buttons()

    def _current(self) -> PriceLabel | None:
        item = self.table.current_item()
        return item if isinstance(item, PriceLabel) else None

    def _refresh_preview(self, *_args) -> None:
        item = self._current()
        if item is None or item.problem and not item.barcode:
            self.preview_label.clear()
            self.preview_hint.setText("")
            self._sync_buttons()
            return
        try:
            image = price_label_print.preview(self.spec, item, zoom=preview_zoom(self.spec))
        except (ValueError, label_print.PrintError) as error:
            self.preview_label.clear()
            self.preview_hint.setText(str(error))
            return
        self.preview_label.setMinimumHeight(self._preview_height())
        self.preview_label.setPixmap(QPixmap.fromImage(image))
        kind = price_labels.encode(item.barcode).kind
        text = f"Штрихкод {kind} · {item.copies} шт."
        if self.spec.layout().clipped:
            text += (" · При таком сдвиге часть этикетки выйдет за край и не "
                     "напечатается — уменьшите сдвиг.")
        self.preview_hint.setText(text)
        self._sync_buttons()

    # --- шаблоны --------------------------------------------------------------

    def _preview_height(self) -> int:
        return round(self.spec.height * 8 * preview_zoom(self.spec)) + 12

    def _fill_templates(self) -> None:
        self.template_box.blockSignals(True)
        self.template_box.clear()
        for name in self.print_settings.names():
            self.template_box.addItem(name, name)
        self.template_box.setCurrentIndex(
            max(0, self.template_box.findData(self.print_settings.template)))
        self.template_box.blockSignals(False)

    def _sync_template_controls(self) -> None:
        """Кнопки и подпись скидки по выбранному шаблону."""
        two = self.spec.prices == 2
        self.delete_button.setEnabled(
            not self.print_settings.is_builtin(self.print_settings.template))
        self.discount_label.setVisible(two)
        self.discount_button.setVisible(two)
        self.discount_label.setText(
            f"Скидка: {self.print_settings.discount:g} %".replace(".", ","))

    def _sync_columns(self) -> None:
        self.table.setColumnHidden(5, self.spec.prices != 2)

    def _recheck_fit(self) -> None:
        """Штрихкод, который не поместится на этой этикетке, помечается до печати."""
        for item in self._everything():
            if item.problem == _FIT_PROBLEM:
                item.problem = ""
            if not item.problem and not price_label_print.fits(item, self.spec):
                item.problem = _FIT_PROBLEM

    def _apply_discount(self) -> None:
        price_labels.apply_discount(
            self._everything(), self.print_settings.discount if self.spec.prices == 2 else None)

    def _everything(self) -> list[PriceLabel]:
        """Список и убранное из него: смена шаблона касается и тех, кого вернут потом."""
        return [*self.labels, *self._removed]

    def _ask_discount(self) -> bool:
        """Спрашивает скидку. Ложь — человек отказался."""
        sample = next((x.price for x in self.labels if x.price is not None), None)
        dialog = DiscountDialog(self.print_settings.discount, sample, self)
        if not dialog.exec():
            return False
        self.print_settings.discount = dialog.percent
        return True

    def _template_picked(self) -> None:
        name = self.template_box.currentData()
        if name == self.print_settings.template:
            return
        chosen = self.print_settings.spec(name)
        # Шаблон с двумя ценами без скидки печатать нечем: спрашиваем сразу, а
        # при отказе остаёмся на прежнем шаблоне, а не на половине выбора.
        if chosen.prices == 2 and not self._ask_discount():
            self._fill_templates()
            return
        self._switch(name)

    def _switch(self, name: str) -> None:
        self.spec = self.print_settings.select(name)
        self._fill_templates()
        for spin, value in ((self.offset_x, self.spec.offset_x),
                            (self.offset_y, self.spec.offset_y)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self._after_spec_change()

    def _after_spec_change(self) -> None:
        self._sync_template_controls()
        self._sync_columns()
        self._recheck_fit()
        self._apply_discount()
        current = self._current()
        self.table.set_items(self.labels)
        if current is not None:
            self.table.select_item(current)
        self._update_metrics()
        self._save_settings()
        self._refresh_preview()
        self._sync_buttons()

    def configure_template(self) -> None:
        name = self.print_settings.template
        dialog = TemplateDialog(
            name, self.spec, builtin=self.print_settings.is_builtin(name),
            sample=self._current(), percent=self.print_settings.discount, parent=self)
        if not dialog.exec():
            return
        edited = dialog.spec()
        if edited.prices == 2 and self.spec.prices != 2 and not self._ask_discount():
            return
        self.print_settings.templates[name] = edited
        self.spec = edited
        for spin, value in ((self.offset_x, edited.offset_x), (self.offset_y, edited.offset_y)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self._after_spec_change()

    def copy_template(self) -> None:
        base = self.print_settings.template
        name, accepted = QInputDialog.getText(
            self, "Новый шаблон", "Название нового шаблона:", text=f"{base}_свой")
        name = name.strip()
        if not accepted or not name:
            return
        if name in self.print_settings.names():
            QMessageBox.warning(self, "Новый шаблон", f"Шаблон «{name}» уже есть.")
            return
        if len(name) > 40:
            QMessageBox.warning(self, "Новый шаблон", "Название длиннее 40 знаков.")
            return
        self.print_settings.add(name, replace(self.spec))
        self._switch(name)
        self.configure_template()

    def delete_template(self) -> None:
        name = self.print_settings.template
        if self.print_settings.is_builtin(name):
            return
        answer = QMessageBox.question(
            self, "Удалить шаблон", f"Удалить шаблон «{name}»?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.print_settings.remove(name)
        self._switch(self.print_settings.template)

    def change_discount(self) -> None:
        if self.spec.prices == 2 and self._ask_discount():
            self._after_spec_change()

    # --- печать ---------------------------------------------------------------

    def _gap_changed(self) -> None:
        self.print_settings.gap_mode = self.gap_box.currentData()
        self._show_gap_hint()
        self._save_settings()

    def _show_gap_hint(self) -> None:
        mode = self.gap_box.currentData()
        text = {
            spec_store.GAP_TEAR: "После каждой номенклатуры печать ждёт, пока вы не "
                                 "оторвёте этикетки. В PDF это не действует.",
            spec_store.GAP_BLANK: "После каждой номенклатуры, кроме последней, идёт "
                                  "пустая этикетка — по ней видно, где рвать.",
        }.get(mode, "")
        self.gap_hint.setText(text)
        # Пустая подпись не должна занимать место между списком и принтером.
        self.gap_hint.setVisible(bool(text))

    def _spec_changed(self) -> None:
        self.spec.offset_x = self.offset_x.value()
        self.spec.offset_y = self.offset_y.value()
        self._refresh_preview()

    def _sync_buttons(self) -> None:
        has = price_label_print.total_copies(self.labels) > 0
        selected = [x for x in self._selected() if x.printable]
        self.print_button.setEnabled(has)
        self.selected_button.setEnabled(bool(selected))
        self.remove_button.setEnabled(bool(self._selected()))
        current = self._current()
        self.test_button.setEnabled(bool(current and not current.problem))

    def _selected(self) -> list[PriceLabel]:
        return [x for x in self.table.selected_items() if isinstance(x, PriceLabel)]

    def _save_settings(self) -> None:
        self.print_settings.templates[self.print_settings.template] = self.spec
        self.print_settings.printer = self.printer_box.currentText()
        try:
            spec_store.save(self.print_settings)
        except OSError:
            pass

    def _target(self) -> tuple[str, str] | None:
        """(принтер, путь PDF). `None` — человек отказался."""
        if self.pdf_box.isChecked():
            path, _ = QFileDialog.getSaveFileName(
                self, "Сохранить этикетки в PDF", "этикетки.pdf", "PDF (*.pdf)")
            if not path:
                return None
            return "", path if path.lower().endswith(".pdf") else path + ".pdf"
        name = self.printer_box.currentText()
        if not name:
            self._fail("Принтер не выбран. Установите термопринтер в Windows "
                       "или отметьте «Сохранить в PDF».")
            return None
        return name, ""

    def print_labels(self, selected_only: bool) -> None:
        items = self._selected() if selected_only else self.labels
        items = [x for x in items if x.printable]
        total = price_label_print.total_copies(items)
        if not total or (target := self._target()) is None:
            return
        self._run_print(items, target, total)

    def print_test(self) -> None:
        item = self._current()
        if item is None or item.problem or (target := self._target()) is None:
            return
        single = replace(item, copies=1)
        self._run_print([single], target, 1)

    def _run_print(self, items: list[PriceLabel], target: tuple[str, str], total: int) -> None:
        # Разделители тоже страницы: без них шкала дошла бы до конца раньше печати.
        blanks = (len(items) - 1 if self.gap_box.currentData() == spec_store.GAP_BLANK
                  else 0)
        progress = QProgressDialog("Печатаем этикетки…", "Остановить", 0, total + blanks, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        def step(done: int, _total: int) -> bool:
            progress.setValue(done)
            QApplication.processEvents()
            return not progress.wasCanceled()

        mode = self.gap_box.currentData()

        def tear(printed: PriceLabel, following: PriceLabel) -> bool:
            progress.hide()
            box = QMessageBox(self)
            box.setWindowTitle("Оторвите этикетки")
            box.setIcon(QMessageBox.Icon.Information)
            box.setText(
                f"Напечатано: {printed.title} — {printed.copies} шт.\n\n"
                "Дождитесь конца печати и оторвите этикетки.\n\n"
                f"Дальше: {following.title} — {following.copies} шт.")
            go = box.addButton("Печатать дальше", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Остановить", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(go)
            box.exec()
            proceed = box.clickedButton() is go
            if proceed:
                progress.show()
            return proceed

        done = 0
        try:
            done = price_label_print.print_labels(
                items, self.spec, target[0], pdf_path=target[1], progress=step,
                blank_between=mode == spec_store.GAP_BLANK,
                pause=tear if mode == spec_store.GAP_TEAR else None)
        except label_print.PrintError as error:
            self._fail(str(error))
        finally:
            progress.close()
        if done:
            self._save_settings()
            where = f"в PDF {target[1]}" if target[1] else f"на принтер «{target[0]}»"
            text = f"Отправлено {where}: {done} из {total}."
            self.status.setStyleSheet("")
            self.status.setText(text)
            self.notify(text, ToastKind.SUCCESS)
