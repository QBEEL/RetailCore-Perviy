"""Вкладка «Заказ»: перенос количеств из выгрузки 1С в бланк поставщика."""
from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core import order, workbook
from ..core.order import OrderLine, OrderSheet, TargetIndex, TargetRow
from ..core.settings import AppSettings
from . import icons
from .tasks import run_task
from .theme import Metrics, Palette
from .widgets.aliases_dialog import AliasesDialog
from .widgets.common import Card, Divider, Hint, MetricTile, SectionTitle, Subtitle, Title, fade_in
from .widgets.file_picker import FilePicker
from .widgets.highlights_dialog import HighlightsDialog
from .widgets.inputs import SelectBox
from .widgets.table import Column, DataTable
from .widgets.toast import ToastKind


_SIMILAR_HINT = "Похожие строки бланка · тестеры помечены ⚠"


class OrderPage(QWidget):
    """Из выгрузки 1С берётся количество, в бланке заполняется «Заказ (шт.)»."""

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, ToastKind], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self.source: OrderSheet | None = None
        self.target: OrderSheet | None = None
        self.index: TargetIndex | None = None
        self.lines: list[OrderLine] = []
        self.highlights: list[TargetRow] = []
        self._suggestions: list[order.Suggestion] = []
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self._refresh_candidates)
        self._build()

    # --- интерфейс ------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Title("Перенос заказа", self))
        root.addWidget(Subtitle(
            "Количества из выгрузки 1С переносятся в бланк заказа поставщика. "
            "Соответствие ищется по артикулу, затем по штрихкоду. Если в книге "
            "несколько листов, нужный выбирается в списке «Лист».", self))

        root.addWidget(self._files_card())
        root.addWidget(self._metrics_row())
        root.addWidget(self._workspace(), 1)

    def _files_card(self) -> Card:
        card = Card(self)
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        body = card.body()

        self.source_picker = FilePicker("Выгрузка из 1С", "не выбрана", card)
        self.source_picker.file_selected.connect(lambda p: self._load(p, source=True))
        self.source_picker.sheet_changed.connect(lambda name: self._reload(source=True, sheet=name))
        self.source_picker.set_recent(self.settings.recent_order_source)
        body.addWidget(self.source_picker)

        self.source_column = SelectBox(card)
        self.source_column.setMinimumWidth(280)
        self.source_column.currentIndexChanged.connect(self._on_source_column)
        self.source_picker.add_control("Колонка с заказом:", self.source_column)

        body.addWidget(Divider(card))

        self.target_picker = FilePicker("Бланк заказа поставщика", "не выбран", card)
        self.target_picker.file_selected.connect(lambda p: self._load(p, source=False))
        self.target_picker.sheet_changed.connect(lambda name: self._reload(source=False, sheet=name))
        self.target_picker.set_recent(self.settings.recent_order_target)
        body.addWidget(self.target_picker)

        self.target_column = SelectBox(card)
        self.target_column.setMinimumWidth(280)
        self.target_column.currentIndexChanged.connect(self._on_target_column)
        self.target_picker.add_control("Куда записывать:", self.target_column)

        body.addWidget(Divider(card))

        actions = QHBoxLayout()
        actions.setSpacing(9)
        self.run_button = QPushButton("Перенести", card)
        self.run_button.setObjectName("Primary")
        self.run_button.setIcon(icons.icon("run", Palette.TEXT_ON_PRIMARY))
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.run_transfer)
        actions.addWidget(self.run_button)

        self.save_button = QPushButton("Сохранить бланк", card)
        self.save_button.setObjectName("Success")
        self.save_button.setIcon(icons.icon("save", Palette.TEXT_ON_PRIMARY))
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save)
        actions.addWidget(self.save_button)

        self.highlights_button = QPushButton("Новинки и хиты", card)
        self.highlights_button.setIcon(icons.icon("star", Palette.WARNING))
        self.highlights_button.setToolTip(
            "Позиции прайса с пометками поставщика, которых нет в заказе")
        self.highlights_button.setEnabled(False)
        self.highlights_button.clicked.connect(self.show_highlights)
        actions.addWidget(self.highlights_button)

        self.status_label = Hint("Выберите оба файла", card)
        actions.addWidget(self.status_label, 1)
        body.addLayout(actions)
        return card

    def _metrics_row(self) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Metrics.GAP)
        self.tile_moved = MetricTile("Перенесено позиций", Palette.SUCCESS, row)
        self.tile_units = MetricTile("Штук в бланке", Palette.PRIMARY, row)
        self.tile_missing = MetricTile("Не найдено позиций", Palette.DANGER, row)
        self.tile_lost = MetricTile("Штук не перенесено", Palette.WARNING, row)
        for tile in (self.tile_moved, self.tile_units, self.tile_missing, self.tile_lost):
            layout.addWidget(tile)
        return row

    def _workspace(self) -> QSplitter:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        # Своего минимума нет: он берётся от карточек, иначе их содержимое
        # сжимается и налезает друг на друга.
        splitter.addWidget(self._lines_card())
        splitter.addWidget(self._manual_card())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([820, 360])
        return splitter

    def _lines_card(self) -> Card:
        card = Card(self, padding=Metrics.PAD - 4)
        body = card.body()
        body.setSpacing(Metrics.GAP - 4)

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Позиции заказа", card))
        self.filter_box = SelectBox(card)
        self.filter_box.addItem("Все", "")
        self.filter_box.addItem("Перенесено", "ok")
        self.filter_box.addItem("Не найдено", "miss")
        self.filter_box.currentIndexChanged.connect(
            lambda: self.table.proxy.set_status(self.filter_box.currentData()))
        header.addWidget(self.filter_box)

        self.search = QLineEdit(card)
        self.search.setPlaceholderText("Поиск по позициям…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        header.addWidget(self.search, 1)
        body.addLayout(header)

        self.table = DataTable(self._columns(), card)
        self.table.setMinimumHeight(110)
        _fill_height(self.table)
        self.table.model_.set_status_provider(lambda l: "ok" if l.matched else "miss")
        self.table.selectionModel().selectionChanged.connect(self._on_selected)
        body.addWidget(self.table, 1)
        return card

    def _columns(self) -> list[Column]:
        return [
            Column("", lambda l: "✓" if l.matched else "—", 34,
                   sort_key=lambda l: l.matched,
                   align=Qt.AlignmentFlag.AlignCenter,
                   color=lambda l: QColor(Palette.SUCCESS if l.matched else Palette.DANGER)),
            Column("Артикул", lambda l: l.article, 148, highlight=True),
            Column("Наименование", lambda l: l.name, 260, highlight=True),
            Column("Характеристика", lambda l: l.trait, 170, highlight=True),
            Column("Заказ, шт.", lambda l: f"{l.quantity:g}", 92,
                   sort_key=lambda l: l.quantity,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            Column("Строка бланка", lambda l: l.target_row or "", 108,
                   sort_key=lambda l: l.target_row or 0,
                   align=Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter),
            Column("Способ", lambda l: l.method, 130),
        ]

    def _manual_card(self) -> Card:
        card = Card(self, padding=Metrics.PAD - 4)
        body = card.body()
        body.setSpacing(Metrics.GAP - 4)
        body.addWidget(SectionTitle("Ручная привязка", card))
        self.selected_label = QLabel("Выберите позицию слева", card)
        self.selected_label.setWordWrap(True)
        self.selected_label.setStyleSheet(f"color: {Palette.TEXT_MUTED};")
        body.addWidget(self.selected_label)

        self.row_search = QLineEdit(card)
        self.row_search.setPlaceholderText("Поиск по бланку: артикул, штрихкод, слова…")
        self.row_search.setClearButtonEnabled(True)
        self.row_search.textChanged.connect(lambda _: self._search_timer.start())
        body.addWidget(self.row_search)

        self.candidates_hint = Hint(_SIMILAR_HINT, card)
        self.candidates_hint.setWordWrap(False)
        body.addWidget(self.candidates_hint)

        self.suggestion_list = QListWidget(card)
        self.suggestion_list.setMinimumHeight(80)
        _fill_height(self.suggestion_list)
        self.suggestion_list.setToolTip(
            "Двойной клик — привязать заказ к строке бланка.\n"
            "Тестеры помечены ⚠ — у них артикул отличается одной буквой.")
        self.suggestion_list.itemDoubleClicked.connect(lambda _: self.apply_manual())
        body.addWidget(self.suggestion_list, 1)

        self.remember_box = QCheckBox("Запоминать привязку как исключение", card)
        self.remember_box.setChecked(True)
        self.remember_box.setToolTip(
            "Название в 1С и в прайсе поставщика могут описывать товар по-разному. "
            "Сохранённая привязка применится к этой позиции при следующих переносах.")
        body.addWidget(self.remember_box)

        bind = QPushButton("Привязать", card)
        bind.setObjectName("Primary")
        bind.setIcon(icons.icon("link", Palette.TEXT_ON_PRIMARY))
        bind.setToolTip("Записать заказ в выбранную строку бланка")
        bind.clicked.connect(self.apply_manual)

        clear = QPushButton("Снять", card)
        clear.setObjectName("Danger")
        clear.setIcon(icons.icon("unlink", Palette.DANGER))
        clear.setToolTip("Убрать привязку выбранной позиции")
        clear.clicked.connect(self.clear_manual)
        body.addLayout(_pair(bind, clear))

        self.aliases_button = QPushButton(card)
        self.aliases_button.setIcon(icons.icon("aliases"))
        self.aliases_button.setToolTip("Сохранённые ручные привязки")
        self.aliases_button.clicked.connect(self.edit_aliases)
        self._update_aliases_button()

        export = QPushButton("Ненайденные", card)
        export.setIcon(icons.icon("export"))
        export.setToolTip("Сохранить список позиций, которых нет в бланке, в отдельный файл")
        export.clicked.connect(self.export_missing)
        body.addLayout(_pair(self.aliases_button, export))
        return card

    # --- загрузка -------------------------------------------------------------

    def _load(self, path: str, *, source: bool) -> None:
        picker = self.source_picker if source else self.target_picker
        picker.set_status("чтение файла…", Palette.PRIMARY)
        try:
            sheets = workbook.list_sheets(path)
        except Exception as error:  # noqa: BLE001
            self._on_failed(str(error), source)
            return
        # Лист, выбранный для этого файла раньше, важнее автоопределения.
        remembered = self.settings.order_sheet_for(path)
        picker.set_sheets(sheets, remembered)
        self.settings.remember_order_file(path, source=source)
        picker.set_recent(self.settings.recent_order_source if source
                          else self.settings.recent_order_target)
        self._reload(source=source, sheet=remembered or None)

    def _reload(self, *, source: bool, sheet: str | None) -> None:
        picker = self.source_picker if source else self.target_picker
        if not picker.path:
            return
        picker.set_status("разбор листа…", Palette.PRIMARY)
        run_task(
            order.open_sheet,
            picker.path,
            sheet,
            source=source,
            on_result=lambda parsed: self._on_loaded(parsed, source),
            on_error=lambda message: self._on_failed(message, source),
        )

    def _on_loaded(self, sheet: OrderSheet, source: bool) -> None:
        picker = self.source_picker if source else self.target_picker
        combo = self.source_column if source else self.target_column
        if source:
            self.source = sheet
        else:
            self.target = sheet
            self.index = None
            self.highlights = []
            self.highlights_button.setEnabled(False)
            self.highlights_button.setText("Новинки и хиты")
            # Индекс строится заранее: поиск по бланку должен работать
            # и до нажатия «Перенести».
            run_task(order.TargetIndex, sheet, on_result=self._on_indexed)
        _select_sheet(picker, sheet.sheet)
        self.settings.remember_order_sheet(sheet.path, sheet.sheet)
        self.settings.save()

        rows = len(sheet.rows) - sheet.first_data_row
        picker.set_status(f"лист «{sheet.sheet}» · {rows} строк", Palette.SUCCESS)
        self._fill_columns(combo, sheet)
        self._update_ready()

    def _fill_columns(self, combo: SelectBox, sheet: OrderSheet) -> None:
        combo.blockSignals(True)
        combo.clear()
        options = sheet.options or []
        for option in options:
            combo.addItem(option.label, option.index)
        # Запасной вариант: показать все колонки, если ни одна не названа «Заказ».
        if not options:
            for index, title in enumerate(sheet.titles):
                combo.addItem(title or f"Колонка {index + 1}", index)
        if sheet.quantity is not None:
            position = combo.findData(sheet.quantity)
            combo.setCurrentIndex(max(position, 0))
        combo.blockSignals(False)
        if combo.currentIndex() >= 0:
            sheet.quantity = combo.currentData()

    def _on_source_column(self) -> None:
        if self.source and self.source_column.currentIndex() >= 0:
            self.source.quantity = self.source_column.currentData()

    def _on_target_column(self) -> None:
        if self.target and self.target_column.currentIndex() >= 0:
            self.target.quantity = self.target_column.currentData()

    def _on_indexed(self, index: TargetIndex) -> None:
        if self.target is not None and index.target is self.target:
            self.index = index

    def _on_failed(self, message: str, source: bool) -> None:
        picker = self.source_picker if source else self.target_picker
        picker.set_status("ошибка чтения", Palette.DANGER)
        self.notify(f"Не удалось разобрать файл: {message}", ToastKind.ERROR)

    def _update_ready(self) -> None:
        ready = self.source is not None and self.target is not None
        self.run_button.setEnabled(ready)
        if ready:
            self.status_label.setText("Готово — нажмите «Перенести»")

    # --- перенос --------------------------------------------------------------

    def run_transfer(self) -> None:
        if not (self.source and self.target):
            return
        if self.target.quantity is None:
            self.notify("В бланке не выбрана колонка для записи", ToastKind.WARNING)
            return
        run_task(
            order.transfer_with_index,
            self.source,
            self.target,
            self.settings.order_aliases,
            on_result=self._on_done,
            on_error=lambda m: self.notify(f"Ошибка переноса: {m}", ToastKind.ERROR),
        )

    def _on_done(self, result: tuple[list[OrderLine], TargetIndex]) -> None:
        lines, self.index = result
        self.lines = lines
        self.table.set_items(lines)
        self.refresh_metrics()
        self._refresh_highlights()
        fade_in(self.table)
        stats = order.summarize(lines)
        self.save_button.setEnabled(stats["перенесено"] > 0)
        self.status_label.setText(
            f"Перенесено {stats['перенесено']:g} из {stats['позиций']:g} позиций")
        if stats["не найдено"]:
            self.notify(
                f"Не найдено в бланке: {stats['не найдено']:g} позиций "
                f"({stats['штук потеряно']:g} шт.) — проверьте правую панель",
                ToastKind.WARNING)
        else:
            self.notify("Все позиции перенесены", ToastKind.SUCCESS)
        if self.highlights:
            self.notify(
                f"В прайсе {len(self.highlights)} позиций с пометками "
                f"{self._mark_names()} — их нет в заказе, можно добавить",
                ToastKind.INFO)

    # --- новинки и хиты -------------------------------------------------------

    def _refresh_highlights(self) -> None:
        taken = {line.target_row for line in self.lines if line.matched}
        self.highlights = self.index.highlights(taken) if self.index else []
        count = len(self.highlights)
        self.highlights_button.setEnabled(count > 0)
        self.highlights_button.setText(f"Новинки и хиты ({count})" if count else "Новинки и хиты")

    def _mark_names(self) -> str:
        names: list[str] = []
        for entry in self.highlights:
            names += [mark for mark in entry.marks if mark not in names]
        return "/".join(names[:3])

    def show_highlights(self) -> None:
        if not self.highlights:
            return
        dialog = HighlightsDialog(self.highlights, self)
        if not dialog.exec():
            return
        chosen = dialog.chosen()
        if not chosen:
            self.notify("Количество не указано — ничего не добавлено", ToastKind.INFO)
            return
        self.lines += [OrderLine.from_target(entry, quantity) for entry, quantity in chosen]
        self.table.set_items(self.lines)
        self.refresh_metrics()
        self._refresh_highlights()
        self.save_button.setEnabled(True)
        units = sum(quantity for _, quantity in chosen)
        self.notify(f"Добавлено в заказ: {len(chosen)} позиций ({units} шт.)", ToastKind.SUCCESS)

    def refresh_metrics(self) -> None:
        stats = order.summarize(self.lines)
        self.tile_moved.set_value(f"{stats['перенесено']:g}")
        self.tile_units.set_value(f"{stats['штук перенесено']:g}")
        self.tile_missing.set_value(f"{stats['не найдено']:g}")
        self.tile_lost.set_value(f"{stats['штук потеряно']:g}")

    # --- ручная привязка ------------------------------------------------------

    def _on_selected(self) -> None:
        line = self.table.current_item()
        if not isinstance(line, OrderLine):
            return
        binding = (f"→ строка {line.target_row} ({line.method})" if line.matched
                   else "→ в бланке не найдена")
        self.selected_label.setText(
            f"{line.title}\nартикул {line.article} · {line.quantity:g} шт.\n{binding}")
        self.selected_label.setStyleSheet(f"color: {Palette.TEXT}; font-weight: 500;")
        self._refresh_candidates()

    def _refresh_candidates(self) -> None:
        """Список справа: результаты поиска по бланку либо похожие строки."""
        query = self.row_search.text().strip()
        line = self.table.current_item()
        if query:
            self._suggestions = self.index.search(query) if self.index else []
            self.candidates_hint.setText(f"Найдено строк бланка: {len(self._suggestions)}")
            empty = "Ничего не найдено — попробуйте часть артикула или одно слово"
        else:
            self._suggestions = line.suggestions if isinstance(line, OrderLine) else []
            self.candidates_hint.setText(_SIMILAR_HINT)
            empty = "Похожих строк нет — найдите строку через поиск выше"

        self.suggestion_list.clear()
        for suggestion in self._suggestions:
            mark = "  ⚠ ТЕСТЕР" if suggestion.tester else ""
            if suggestion.marks:
                mark += "  ★ " + "/".join(suggestion.marks)
            item = QListWidgetItem(
                f"стр. {suggestion.row}  ·  {suggestion.article}  ·  {suggestion.name}{mark}")
            if suggestion.tester:
                item.setForeground(QColor(Palette.WARNING))
            item.setToolTip(
                f"{suggestion.name}\nартикул {suggestion.article} · {suggestion.ean}"
                f"\nсходство {suggestion.score:.0f}%")
            self.suggestion_list.addItem(item)
        if not self._suggestions:
            self.suggestion_list.addItem(QListWidgetItem(empty))

    def apply_manual(self) -> None:
        line = self.table.current_item()
        row = self.suggestion_list.currentRow()
        if not isinstance(line, OrderLine):
            self.notify("Выберите позицию в таблице", ToastKind.WARNING)
            return
        if not 0 <= row < len(self._suggestions):
            self.notify("Выберите строку бланка в списке", ToastKind.WARNING)
            return
        suggestion = self._suggestions[row]
        line.assign(suggestion.row)
        self.table.model_.refresh_item(line)
        self.refresh_metrics()
        self.save_button.setEnabled(True)
        remembered = self._remember_alias(line, suggestion.row)
        warning = " (это ТЕСТЕР)" if suggestion.tester else ""
        note = " · сохранено в исключения" if remembered else ""
        self.notify(f"Привязано к строке {suggestion.row}{warning}{note}",
                    ToastKind.WARNING if suggestion.tester else ToastKind.SUCCESS)
        self._on_selected()

    def _remember_alias(self, line: OrderLine, row: int) -> bool:
        if not (self.remember_box.isChecked() and self.index):
            return False
        self.settings.order_aliases.remember(line, self.index.info(row))
        self.settings.save()
        self._update_aliases_button()
        return True

    def clear_manual(self) -> None:
        line = self.table.current_item()
        if not isinstance(line, OrderLine):
            return
        if line.added:
            # Позиции из прайса нет в выгрузке — без строки бланка она бессмысленна.
            self.lines.remove(line)
            self.table.set_items(self.lines)
            self.notify(f"Позиция убрана из заказа: {line.title[:60]}", ToastKind.INFO)
        else:
            line.clear()
            self.table.model_.refresh_item(line)
        self.refresh_metrics()
        self._refresh_highlights()
        self._on_selected()

    def edit_aliases(self) -> None:
        dialog = AliasesDialog(self.settings.order_aliases, self)
        dialog.exec()
        self.settings.save()
        self._update_aliases_button()

    def _update_aliases_button(self) -> None:
        count = len(self.settings.order_aliases)
        self.aliases_button.setText(f"Исключения ({count})" if count else "Исключения")

    def _filter(self, text: str) -> None:
        self.table.proxy.set_text(text)
        self.table.model_.set_terms(text.casefold().replace("ё", "е").split())

    # --- сохранение -----------------------------------------------------------

    def save(self) -> None:
        if not self.lines or self.target is None:
            return
        base = os.path.splitext(os.path.basename(self.target.path))[0]
        suggested = os.path.join(os.path.dirname(self.target.path), f"{base}_заполнен.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить бланк", suggested, "Excel (*.xlsx)")
        if not path:
            return
        updates = order.build_updates(self.lines, self.target)
        run_task(
            workbook.write_values,
            self.target.path,
            path,
            updates,
            True,                      # колонка заказа перезаписывается целиком
            self.target.sheet,
            on_result=lambda written: self._on_saved(written, path),
            on_error=lambda m: self.notify(f"Не удалось сохранить: {m}", ToastKind.ERROR),
        )

    def _on_saved(self, written: int, path: str) -> None:
        self.notify(f"Записано {written} строк. Файл: {os.path.basename(path)}", ToastKind.SUCCESS)
        self.status_label.setText(f"Сохранено: {path}")

    def export_missing(self) -> None:
        missing = [line for line in self.lines if not line.matched]
        if not missing:
            self.notify("Все позиции перенесены — выгружать нечего", ToastKind.INFO)
            return
        suggested = os.path.join(
            os.path.dirname(self.target.path) if self.target else "", "Не перенесено.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить список", suggested, "Excel (*.xlsx)")
        if not path:
            return
        try:
            _write_missing(missing, path)
        except Exception as error:  # noqa: BLE001
            self.notify(f"Не удалось сохранить список: {error}", ToastKind.ERROR)
            return
        self.notify(f"Список сохранён: {os.path.basename(path)}", ToastKind.SUCCESS)


def _fill_height(widget: QWidget) -> None:
    """Виджет занимает всю свободную высоту, но не требует её.

    Своя предпочитаемая высота у таблиц и списков большая (192 px), из-за неё
    страница просила бы больше места, чем есть на экране, и получала полосу
    прокрутки при полностью помещающемся содержимом. Нижняя граница остаётся
    заданной через setMinimumHeight.
    """
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)


def _pair(left: QWidget, right: QWidget) -> QHBoxLayout:
    """Две кнопки в строку: в узкой панели вертикальный столбец кнопок не помещается."""
    row = QHBoxLayout()
    row.setSpacing(7)
    row.addWidget(left)
    row.addWidget(right)
    return row


def _select_sheet(picker: FilePicker, name: str) -> None:
    """Показывает в списке лист, который выбрало автоопределение, без перезагрузки."""
    box = picker.sheet_box
    if name and box.currentText() != name:
        box.blockSignals(True)
        box.setCurrentText(name)
        box.blockSignals(False)


def _write_missing(lines: list[OrderLine], path: str) -> None:
    """Выгружает позиции, которых нет в бланке, чтобы запросить их у поставщика."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Не перенесено"
    sheet.append(["Артикул", "Штрихкод", "Наименование", "Характеристика",
                  "Заказ, шт.", "Строка в выгрузке"])
    for line in lines:
        sheet.append([line.article, line.ean, line.name, line.trait,
                      line.quantity, line.source_row])
    for column, width in zip("ABCDEF", (20, 18, 52, 28, 12, 18)):
        sheet.column_dimensions[column].width = width
    book.save(path)
