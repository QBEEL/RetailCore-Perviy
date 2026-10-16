"""Вкладка «Этикетки»: ценники 30×20 для обклейки товара из файла номенклатуры."""
from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
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
from .widgets.table import Column, DataTable
from .widgets.toast import ToastKind

_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_PREVIEW_ZOOM = 2


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
        self.print_settings = spec_store.load()
        self.spec = self.print_settings.spec()
        self._build()

    # --- интерфейс ------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Title("Этикетки", self))
        root.addWidget(Subtitle(
            "Ценники для обклейки на термопринтер 30×20 мм. Файл Excel: "
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
        body.addLayout(header)

        self.table = DataTable([
            Column("Строка", lambda x: x.row, 56, align=_RIGHT),
            Column("Номенклатура", lambda x: x.name, 205, highlight=True),
            Column("Характеристика", lambda x: x.variant, 135, highlight=True),
            Column("Штрихкод", lambda x: x.barcode, 112),
            Column("Цена", lambda x: price_labels.format_price(x.price), 58,
                   sort_key=lambda x: x.price or 0, align=_RIGHT),
            Column("Кол-во", lambda x: x.copies, 62, align=_RIGHT),
            Column("Состояние", lambda x: x.problem or "", 150, color=self._problem_color),
        ], card)
        self.table.item_activated.connect(self._edit_copies)
        self.table.selectionModel().selectionChanged.connect(self._refresh_preview)
        body.addWidget(self.table, 1)
        body.addWidget(Hint("Двойной щелчок по строке — изменить количество этикеток.", card))
        return card

    @staticmethod
    def _problem_color(item: PriceLabel) -> QColor | None:
        return QColor(Palette.DANGER) if item.problem else None

    def _print_card(self) -> Card:
        card = Card(self, padding=Metrics.PAD - 4)
        body = card.body()
        body.setSpacing(Metrics.GAP - 4)
        body.addWidget(SectionTitle("Печать", card))

        self.preview_label = QLabel(card)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(round(self.spec.height * 8 * _PREVIEW_ZOOM) + 12)
        self.preview_label.setStyleSheet(
            f"background: {Palette.SURFACE_ALT}; border-radius: {Metrics.RADIUS_SM}px;")
        body.addWidget(self.preview_label)
        self.preview_hint = Hint("", card)
        self.preview_hint.setWordWrap(True)
        body.addWidget(self.preview_hint)

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
        self.name_font = self._spin(self.spec.name_font, 3.5, 10, 0.5)
        for caption, spin in (("Сдвиг вправо, мм", self.offset_x),
                              ("Сдвиг вниз, мм", self.offset_y),
                              ("Шрифт названия, пт", self.name_font)):
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

        for spin in (self.offset_x, self.offset_y, self.name_font):
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
        for item in items:
            # Штрихкод, который не поместится, ловим при показе, а не на печати.
            if not item.problem and not price_label_print.fits(item, self.spec):
                item.problem = "штрихкод слишком длинный"
        self.labels = items
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
            image = price_label_print.preview(self.spec, item, zoom=_PREVIEW_ZOOM)
        except (ValueError, label_print.PrintError) as error:
            self.preview_label.clear()
            self.preview_hint.setText(str(error))
            return
        self.preview_label.setPixmap(QPixmap.fromImage(image))
        kind = price_labels.encode(item.barcode).kind
        self.preview_hint.setText(f"Штрихкод {kind} · {item.copies} шт.")
        self._sync_buttons()

    # --- печать ---------------------------------------------------------------

    def _spec_changed(self) -> None:
        self.spec.offset_x = self.offset_x.value()
        self.spec.offset_y = self.offset_y.value()
        self.spec.name_font = self.name_font.value()
        self._refresh_preview()

    def _sync_buttons(self) -> None:
        has = price_label_print.total_copies(self.labels) > 0
        selected = [x for x in self._selected() if x.printable]
        self.print_button.setEnabled(has)
        self.selected_button.setEnabled(bool(selected))
        current = self._current()
        self.test_button.setEnabled(bool(current and not current.problem))

    def _selected(self) -> list[PriceLabel]:
        return [x for x in self.table.selected_items() if isinstance(x, PriceLabel)]

    def _save_settings(self) -> None:
        self.print_settings.label = self.spec
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
        single = PriceLabel(item.row, item.name, item.variant, item.barcode,
                            item.price, 1)
        self._run_print([single], target, 1)

    def _run_print(self, items: list[PriceLabel], target: tuple[str, str], total: int) -> None:
        progress = QProgressDialog("Печатаем этикетки…", "Остановить", 0, total, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        def step(done: int, _total: int) -> bool:
            progress.setValue(done)
            QApplication.processEvents()
            return not progress.wasCanceled()

        done = 0
        try:
            done = price_label_print.print_labels(
                items, self.spec, target[0], pdf_path=target[1], progress=step)
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
