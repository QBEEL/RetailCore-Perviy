"""Страница сопоставления: каталог + целевой файл → проверка → сохранение."""
from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core import workbook
from ..core.matching import Matcher, build_updates, summarize
from ..core.models import FieldRole, MatchResult, MatchStatus, Record, Sheet
from ..core.search import SearchEngine
from ..core.settings import AppSettings
from . import icons, snapshot_task
from .tasks import run_task
from .theme import Metrics, Palette, STATUS_COLORS, score_color
from .widgets.common import Card, Divider, Hint, MetricTile, SectionTitle, Subtitle, Title, fade_in
from .widgets.catalog_list import CatalogList
from .widgets.file_picker import FilePicker
from .widgets.inputs import SelectBox
from .widgets.product_card import ProductCard
from .widgets.table import Column, DataTable
from .widgets.toast import ToastKind

STATUS_ICONS = {
    MatchStatus.MATCHED: "✓",
    MatchStatus.REVIEW: "!",
    MatchStatus.AMBIGUOUS: "?",
    MatchStatus.MANUAL: "✎",
    MatchStatus.NOT_FOUND: "—",
}


class MatchPage(QWidget):
    """Основной сценарий: сопоставить строки цели с записями каталога."""

    busy_changed = Signal(bool)

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, ToastKind], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self.source: Sheet | None = None
        self.target: Sheet | None = None
        self.extra: dict[str, Sheet] = {}
        self.results: list[MatchResult] = []
        self.engine = SearchEngine(config=settings.search)
        self._candidates: list[Record] = []
        self._candidate_origins: list[str] = []
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self._run_manual_search)

        self._build()

    # --- построение интерфейса -------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Title("Сопоставление файлов", self))
        root.addWidget(Subtitle(
            "Каталог — источник артикулов, EAN и цен. Целевой файл — таблица, "
            "в которой эти данные нужно проставить.", self))

        root.addWidget(self._files_card())
        root.addWidget(self._metrics_row())
        root.addWidget(self._workspace(), 1)

    def _files_card(self) -> Card:
        card = Card(self)
        # Карточка занимает ровно столько, сколько нужно содержимому: иначе при
        # нехватке места Qt сжимает поля выбора файлов и обрезает их.
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        body = card.body()

        self.source_picker = FilePicker("Каталог (источник данных)", "не выбран", card)
        self.source_picker.file_selected.connect(lambda p: self._load(p, source=True))
        self.source_picker.sheet_changed.connect(lambda _: self._reload(source=True))
        self.source_picker.set_recent(self.settings.recent_source)
        body.addWidget(self.source_picker)

        self.extra_catalogs = CatalogList(card)
        self.extra_catalogs.set_paths(self.settings.extra_sources)
        self.extra_catalogs.changed.connect(self._on_extra_changed)
        body.addWidget(self.extra_catalogs)

        body.addWidget(Divider(card))

        self.target_picker = FilePicker("Целевой файл (что заполняем)", "не выбран", card)
        self.target_picker.file_selected.connect(lambda p: self._load(p, source=False))
        self.target_picker.sheet_changed.connect(lambda _: self._reload(source=False))
        self.target_picker.set_recent(self.settings.recent_target)
        body.addWidget(self.target_picker)

        body.addWidget(Divider(card))

        actions = QHBoxLayout()
        actions.setSpacing(9)

        self.run_button = QPushButton("Сопоставить", card)
        self.run_button.setObjectName("Primary")
        self.run_button.setIcon(icons.icon("run", Palette.TEXT_ON_PRIMARY))
        self.run_button.setToolTip("Запустить автосопоставление (F5)")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.run_matching)
        actions.addWidget(self.run_button)

        self.save_button = QPushButton("Сохранить результат", card)
        self.save_button.setObjectName("Success")
        self.save_button.setIcon(icons.icon("save", Palette.TEXT_ON_PRIMARY))
        self.save_button.setToolTip("Записать найденные данные в копию целевого файла (Ctrl+S)")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_results)
        actions.addWidget(self.save_button)

        self.progress = QProgressBar(card)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        actions.addWidget(self.progress, 1)

        self.status_label = Hint("Выберите оба файла, чтобы начать", card)
        actions.addWidget(self.status_label, 2)
        body.addLayout(actions)
        return card

    def _metrics_row(self) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Metrics.GAP)
        self.tiles: dict[MatchStatus, MetricTile] = {}
        for status in (MatchStatus.MATCHED, MatchStatus.REVIEW, MatchStatus.AMBIGUOUS,
                       MatchStatus.MANUAL, MatchStatus.NOT_FOUND):
            tile = MetricTile(status.title, STATUS_COLORS[status.value][0], row)
            self.tiles[status] = tile
            layout.addWidget(tile)
        return row

    def _workspace(self) -> QSplitter:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        # Рабочая область уступает место карточке файлов: у таблицы есть
        # прокрутка, а у полей выбора файлов — нет.
        splitter.setMinimumHeight(220)
        splitter.addWidget(self._results_card())
        splitter.addWidget(self._manual_card())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes(self.settings.splitter_sizes or [820, 360])
        self.splitter = splitter
        return splitter

    def _results_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Результаты", card))

        self.filter_box = SelectBox(card)
        self.filter_box.addItem("Все строки", "")
        for status in MatchStatus:
            self.filter_box.addItem(status.title, status.value)
        self.filter_box.currentIndexChanged.connect(
            lambda: self.table.proxy.set_status(self.filter_box.currentData()))
        header.addWidget(self.filter_box)

        self.table_search = QLineEdit(card)
        self.table_search.setPlaceholderText("Поиск в таблице…")
        self.table_search.setClearButtonEnabled(True)
        self.table_search.textChanged.connect(self._filter_table)
        self.table_search.setMinimumWidth(220)
        header.addWidget(self.table_search, 1)
        body.addLayout(header)

        self.table = DataTable(self._columns(), card)
        self.table.setMinimumHeight(120)
        self.table.model_.set_status_provider(lambda r: r.status.value)
        self.table.selectionModel().selectionChanged.connect(self._on_row_selected)
        self.table.item_activated.connect(self._open_card)
        body.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.table_hint = Hint("Двойной клик — карточка товара · Ctrl+C — копировать выделение", card)
        footer.addWidget(self.table_hint, 1)
        body.addLayout(footer)
        return card

    def _columns(self) -> list[Column]:
        return [
            Column("", lambda r: STATUS_ICONS[r.status], 34,
                   sort_key=lambda r: r.status.value,
                   align=Qt.AlignmentFlag.AlignCenter,
                   color=lambda r: QColor(STATUS_COLORS[r.status.value][0])),
            Column("Строка", lambda r: r.target.row, 62,
                   align=Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter),
            Column("Наименование", lambda r: r.target.label, 300, highlight=True),
            Column("Объём", lambda r: str(r.target.quantity or r.target.text(FieldRole.VOLUME)), 96),
            Column("EAN", lambda r: r.source.text(FieldRole.EAN) if r.source else "", 130),
            Column("Артикул", lambda r: r.source.text(FieldRole.ARTICLE) if r.source else "", 140),
            Column("Цена", lambda r: r.source.get(FieldRole.PRICE, "") if r.source else "", 78,
                   sort_key=lambda r: _number(r.source.get(FieldRole.PRICE) if r.source else None),
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            Column("Совпадение", lambda r: f"{r.score:.0f}%" if r.source else "", 92,
                   sort_key=lambda r: r.score,
                   align=Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
                   color=lambda r: score_color(r.score) if r.source else None),
            Column("Метод", lambda r: r.stage, 150),
            Column("Каталог", lambda r: r.origin, 170),
        ]

    def _manual_card(self) -> Card:
        card = Card(self)
        body = card.body()
        body.addWidget(SectionTitle("Ручная привязка", card))

        self.selected_label = QLabel("Выберите строку в таблице", card)
        self.selected_label.setWordWrap(True)
        self.selected_label.setStyleSheet(f"color: {Palette.TEXT_MUTED};")
        body.addWidget(self.selected_label)
        body.addWidget(Divider(card))

        self.manual_search = QLineEdit(card)
        self.manual_search.setPlaceholderText("Поиск по каталогу…")
        self.manual_search.setClearButtonEnabled(True)
        self.manual_search.textChanged.connect(lambda: self._search_timer.start())
        body.addWidget(self.manual_search)

        self.candidate_list = QListWidget(card)
        self.candidate_list.setMinimumHeight(80)
        self.candidate_list.itemDoubleClicked.connect(lambda _: self.apply_manual())
        self.candidate_list.setToolTip("Двойной клик — привязать запись к выбранной строке")
        body.addWidget(self.candidate_list, 1)

        apply_button = QPushButton("Привязать выбранное", card)
        apply_button.setObjectName("Primary")
        apply_button.setIcon(icons.icon("link", Palette.TEXT_ON_PRIMARY))
        apply_button.clicked.connect(self.apply_manual)
        body.addWidget(apply_button)

        clear_button = QPushButton("Сбросить привязку", card)
        clear_button.setObjectName("Danger")
        clear_button.setIcon(icons.icon("unlink", Palette.DANGER))
        clear_button.clicked.connect(self.clear_match)
        body.addWidget(clear_button)
        return card

    # --- загрузка файлов -------------------------------------------------------

    def _load(self, path: str, *, source: bool) -> None:
        picker = self.source_picker if source else self.target_picker
        picker.set_status("загрузка…", Palette.PRIMARY)
        try:
            sheets = workbook.list_sheets(path)
        except Exception as error:  # noqa: BLE001
            picker.set_status("не удалось открыть", Palette.DANGER)
            self.notify(f"Не удалось открыть файл: {error}", ToastKind.ERROR)
            return
        picker.set_sheets(sheets, picker.sheet)
        self.settings.remember_file(path, source=source)
        picker.set_recent(self.settings.recent_source if source else self.settings.recent_target)
        self._reload(source=source)

    def _reload(self, *, source: bool) -> None:
        picker = self.source_picker if source else self.target_picker
        if not picker.path:
            return
        self.busy_changed.emit(True)
        run_task(
            workbook.load_sheet,
            picker.path,
            picker.sheet or None,
            role_overrides=self.settings.overrides_for(picker.path) or None,
            on_result=lambda sheet: self._on_loaded(sheet, source),
            on_error=lambda message: self._on_load_failed(message, source),
        )

    def _on_loaded(self, sheet: Sheet, source: bool) -> None:
        self.busy_changed.emit(False)
        picker = self.source_picker if source else self.target_picker
        picker.set_status(f"{len(sheet.records)} строк · лист «{sheet.sheet_name}»", Palette.SUCCESS)
        if source:
            self.source = sheet
            self.engine.index(sheet)
            # Историю ведём только по каталогам: целевой файл — это то, что
            # заполняется, а не источник цен и ассортимента.
            snapshot_task.capture(sheet, self.settings, self.notify,
                                  status=lambda text: self._snapshot_status(picker, sheet, text))
        else:
            self.target = sheet
        self._update_ready()
        recognized = sum(1 for c in sheet.columns if c.role.value != "other")
        self.notify(
            f"{os.path.basename(sheet.path)}: {len(sheet.records)} строк, распознано полей — {recognized}",
            ToastKind.INFO)

    def _snapshot_status(self, picker: FilePicker, sheet: Sheet, text: str) -> None:
        """Дописывает ход сохранения снимка к строке состояния файла."""
        base = f"{len(sheet.records)} строк · лист «{sheet.sheet_name}»"
        picker.set_status(f"{base} · {text}" if text else base, Palette.SUCCESS)

    def _on_load_failed(self, message: str, source: bool) -> None:
        self.busy_changed.emit(False)
        picker = self.source_picker if source else self.target_picker
        picker.set_status("ошибка загрузки", Palette.DANGER)
        self.notify(f"Ошибка чтения файла: {message}", ToastKind.ERROR)

    def _update_ready(self) -> None:
        ready = self.source is not None and self.target is not None
        self.run_button.setEnabled(ready)
        if ready:
            self.status_label.setText("Готово к сопоставлению — нажмите «Сопоставить» или F5")

    # --- дополнительные каталоги ----------------------------------------------

    @property
    def catalogs(self) -> list[Sheet]:
        """Основной каталог и загруженные дополнительные — в порядке приоритета."""
        if self.source is None:
            return []
        return [self.source] + [s for p in self.extra_catalogs.paths if (s := self.extra.get(p))]

    def _on_extra_changed(self, paths: list[str]) -> None:
        self.settings.extra_sources = list(paths)
        self.settings.save()
        for path in [p for p in self.extra if p not in paths]:
            del self.extra[path]
        for path in paths:
            if path not in self.extra:
                self._load_extra(path)

    def _load_extra(self, path: str) -> None:
        self.extra_catalogs.set_status(path, "загрузка…")
        run_task(
            workbook.load_sheet,
            path,
            None,
            role_overrides=self.settings.overrides_for(path) or None,
            on_result=lambda sheet, p=path: self._on_extra_loaded(p, sheet),
            on_error=lambda message, p=path: self.extra_catalogs.set_status(p, f"ошибка: {message}"),
        )

    def _on_extra_loaded(self, path: str, sheet: Sheet) -> None:
        self.extra[path] = sheet
        base = f"{len(sheet.records)} строк · лист «{sheet.sheet_name}»"
        self.extra_catalogs.set_status(path, base)
        snapshot_task.capture(
            sheet, self.settings, self.notify,
            status=lambda text, p=path, b=base: self.extra_catalogs.set_status(
                p, f"{b} · {text}" if text else b))

    # --- сопоставление ---------------------------------------------------------

    def run_matching(self) -> None:
        if not (self.source and self.target):
            self.notify("Сначала выберите каталог и целевой файл", ToastKind.WARNING)
            return
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.run_button.setEnabled(False)
        self.busy_changed.emit(True)
        self.status_label.setText("Сопоставление…")

        matcher = Matcher(self.catalogs, self.settings.match)
        run_task(
            matcher.match_all,
            self.target.records,
            on_result=self._on_matched,
            on_error=self._on_match_failed,
            on_progress=self._on_progress,
        )

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)

    def _on_matched(self, results: list[MatchResult]) -> None:
        self.results = results
        self.busy_changed.emit(False)
        self.run_button.setEnabled(True)
        self.progress.setVisible(False)
        self.table.set_items(results)
        # Колонка источника осмысленна только когда каталогов несколько.
        self.table.setColumnHidden(len(self.table.model_.columns) - 1, len(self.catalogs) <= 1)
        self.refresh_metrics()
        fade_in(self.table)
        counts = summarize(results)
        found = len(results) - counts[MatchStatus.NOT_FOUND]
        self.save_button.setEnabled(found > 0)
        self.status_label.setText(
            f"Обработано {len(results)} строк · найдено {found} · "
            f"требует внимания {counts[MatchStatus.REVIEW] + counts[MatchStatus.AMBIGUOUS]}")
        self.notify(f"Сопоставление завершено: {found} из {len(results)}", ToastKind.SUCCESS)

    def _on_match_failed(self, message: str) -> None:
        self.busy_changed.emit(False)
        self.run_button.setEnabled(True)
        self.progress.setVisible(False)
        self.status_label.setText("Сопоставление не выполнено")
        self.notify(f"Ошибка сопоставления: {message}", ToastKind.ERROR)

    def refresh_metrics(self) -> None:
        counts = summarize(self.results)
        for status, tile in self.tiles.items():
            tile.set_value(counts[status])

    # --- ручная привязка -------------------------------------------------------

    def _on_row_selected(self) -> None:
        result = self.table.current_item()
        if not isinstance(result, MatchResult):
            return
        quantity = result.target.quantity
        suffix = f" · {quantity}" if quantity else ""
        self.selected_label.setText(f"{result.target.label}{suffix}")
        self.selected_label.setStyleSheet(f"color: {Palette.TEXT}; font-weight: 500;")
        if result.alternatives:
            self._show_candidates(result.alternatives, "вариант для проверки")
            self.manual_search.blockSignals(True)
            self.manual_search.setText(result.target.label)
            self.manual_search.blockSignals(False)
        else:
            self.manual_search.setText(result.target.label)

    def _run_manual_search(self) -> None:
        query = self.manual_search.text().strip()
        if not query or self.source is None:
            self.candidate_list.clear()
            return
        # Ручной поиск охватывает все каталоги: позиции, которой нет в основном,
        # может найтись в архивном прайсе.
        hits: list[tuple[object, str]] = []
        for sheet in self.catalogs:
            self.engine.index(sheet)
            origin = os.path.basename(sheet.path)
            hits.extend((hit, origin) for hit in self.engine.search(query, limit=60))
        hits.sort(key=lambda pair: -pair[0].score)

        self._candidates = [hit.record for hit, _ in hits[:60]]
        self._candidate_origins = [origin for _, origin in hits[:60]]
        self.candidate_list.clear()
        for hit, origin in hits[:60]:
            item = QListWidgetItem(_candidate_text(hit.record, hit.score))
            item.setToolTip(f"{hit.record.label}\n{hit.explanation} · {hit.score:.0f}%\nИсточник: {origin}")
            self.candidate_list.addItem(item)
        if not hits:
            self.candidate_list.addItem(QListWidgetItem("Ничего не найдено"))

    def _show_candidates(self, candidates: list, reason: str) -> None:
        self._candidates = [c.record for c in candidates]
        self._candidate_origins = [c.origin for c in candidates]
        self.candidate_list.clear()
        for candidate in candidates:
            item = QListWidgetItem(_candidate_text(candidate.record, candidate.score))
            item.setToolTip(f"{candidate.record.label}\n{reason}\nИсточник: {candidate.origin}")
            self.candidate_list.addItem(item)

    def apply_manual(self) -> None:
        result = self.table.current_item()
        row = self.candidate_list.currentRow()
        if not isinstance(result, MatchResult):
            self.notify("Выберите строку в таблице результатов", ToastKind.WARNING)
            return
        if not 0 <= row < len(self._candidates):
            self.notify("Выберите запись в списке каталога", ToastKind.WARNING)
            return
        origin = self._candidate_origins[row] if row < len(self._candidate_origins) else ""
        result.assign(self._candidates[row], origin=origin)
        self._refresh_row(result)
        self.save_button.setEnabled(True)
        self.notify("Привязка сохранена", ToastKind.SUCCESS, )

    def clear_match(self) -> None:
        result = self.table.current_item()
        if not isinstance(result, MatchResult):
            return
        result.clear()
        self._refresh_row(result)
        self.notify("Привязка снята", ToastKind.INFO)

    def _refresh_row(self, result: MatchResult) -> None:
        self.table.model_.refresh_item(result)
        self.refresh_metrics()

    def _open_card(self, result: object) -> None:
        if isinstance(result, MatchResult):
            record = result.source or result.target
            sheet = self.source if result.source else self.target
            ProductCard(record, sheet, self).exec()

    def _filter_table(self, text: str) -> None:
        self.table.proxy.set_text(text)
        self.table.model_.set_terms(text.casefold().replace("ё", "е").split())

    # --- сохранение ------------------------------------------------------------

    def save_results(self) -> None:
        if not self.results or self.target is None:
            self.notify("Сначала выполните сопоставление", ToastKind.WARNING)
            return
        base = os.path.splitext(os.path.basename(self.target.path))[0]
        suggested = os.path.join(os.path.dirname(self.target.path), f"{base}_заполнено.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить результат", suggested, "Excel (*.xlsx)")
        if not path:
            return

        updates = build_updates(self.results, self.target, self.settings.fill_roles)
        if not updates:
            self.notify("Нет данных для записи: в целевом файле нет подходящих колонок", ToastKind.WARNING)
            return
        self.busy_changed.emit(True)
        run_task(
            workbook.write_values,
            self.target.path,
            path,
            updates,
            self.settings.overwrite_filled,
            self.target.sheet_name,
            on_result=lambda written: self._on_saved(written, path),
            on_error=self._on_save_failed,
        )

    def _on_saved(self, written: int, path: str) -> None:
        self.busy_changed.emit(False)
        self.notify(f"Записано ячеек: {written}. Файл: {os.path.basename(path)}", ToastKind.SUCCESS)
        self.status_label.setText(f"Сохранено: {path}")

    def _on_save_failed(self, message: str) -> None:
        self.busy_changed.emit(False)
        self.notify(f"Не удалось сохранить: {message}", ToastKind.ERROR)

    # --- восстановление состояния ---------------------------------------------

    def restore(self) -> None:
        """Подставляет последние файлы, но не загружает их без действия пользователя."""
        if self.settings.recent_source:
            self.source_picker.field.setText(self.settings.recent_source[0])
        if self.settings.recent_target:
            self.target_picker.field.setText(self.settings.recent_target[0])

    def open_last_session(self) -> None:
        if self.settings.recent_source and os.path.exists(self.settings.recent_source[0]):
            self.source_picker.set_path(self.settings.recent_source[0])
        if self.settings.recent_target and os.path.exists(self.settings.recent_target[0]):
            self.target_picker.set_path(self.settings.recent_target[0])


def _candidate_text(record: Record, score: float | None) -> str:
    parts = [record.label]
    if article := record.text(FieldRole.ARTICLE):
        parts.append(article)
    if price := record.get(FieldRole.PRICE):
        parts.append(f"{price}")
    prefix = f"[{score:.0f}%] " if score is not None else ""
    return prefix + "  ·  ".join(parts)


def _number(value: object) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return -1.0
