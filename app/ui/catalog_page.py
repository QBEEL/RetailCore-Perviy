"""Страница каталога: умный поиск по загруженному файлу с оценкой совпадения."""
from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import workbook
from ..core.models import FieldRole, Record, Sheet
from ..core.search import SearchEngine, SearchHit
from ..core.settings import AppSettings
from . import icons, snapshot_task
from .tasks import run_task
from .theme import Metrics, Palette, score_color
from .widgets.common import Card, Hint, SectionTitle, Subtitle, Title, fade_in
from .widgets.file_picker import FilePicker
from .widgets.product_card import ProductCard
from .widgets.table import Column, DataTable
from .widgets.toast import ToastKind


class CatalogPage(QWidget):
    """Просмотр и поиск по любому прайс-листу без сопоставления."""

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, ToastKind], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self.sheet: Sheet | None = None
        self.engine = SearchEngine(config=settings.search)
        self._hits: list[SearchHit] = []

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(180)
        self._timer.timeout.connect(self._search)

        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Title("Каталог", self))
        root.addWidget(Subtitle(
            "Откройте любой прайс-лист и ищите по нему: частичные совпадения, "
            "несколько слов и опечатки учитываются автоматически.", self))

        card = Card(self)
        self.picker = FilePicker("Файл каталога", "не выбран", card)
        self.picker.file_selected.connect(self._load)
        self.picker.sheet_changed.connect(lambda _: self._reload())
        self.picker.set_recent(self.settings.recent_source)
        card.body().addWidget(self.picker)
        root.addWidget(card)

        results = Card(self)
        body = results.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Поиск", results))

        self.query = QLineEdit(results)
        self.query.setPlaceholderText("Введите артикул, название или его часть…   (Ctrl+F)")
        self.query.setClearButtonEnabled(True)
        self.query.textChanged.connect(lambda: self._timer.start())
        self.query.setMinimumWidth(320)
        header.addWidget(self.query, 1)

        self.reset_button = QPushButton("Показать все", results)
        self.reset_button.setIcon(icons.icon("reset"))
        self.reset_button.clicked.connect(self._show_all)
        header.addWidget(self.reset_button)
        body.addLayout(header)

        self.summary = Hint("Файл не загружен", results)
        body.addWidget(self.summary)

        self.table = DataTable(self._columns(), results)
        self.table.item_activated.connect(self._open_card)
        body.addWidget(self.table, 1)
        root.addWidget(results, 1)

    def _columns(self) -> list[Column]:
        return [
            Column("Строка", lambda h: h.record.row, 62,
                   align=Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter),
            Column("Наименование", lambda h: h.record.label, 340, highlight=True),
            Column("Артикул", lambda h: h.record.text(FieldRole.ARTICLE), 150, highlight=True),
            Column("EAN", lambda h: h.record.text(FieldRole.EAN), 135, highlight=True),
            Column("Объём", lambda h: str(h.record.quantity or h.record.text(FieldRole.VOLUME)), 92),
            Column("Цена", lambda h: h.record.get(FieldRole.PRICE, ""), 82,
                   sort_key=lambda h: _number(h.record.get(FieldRole.PRICE)),
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            Column("Совпадение", lambda h: f"{h.score:.0f}%" if h.score else "", 96,
                   sort_key=lambda h: h.score,
                   align=Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter,
                   color=lambda h: score_color(h.score) if h.score else None),
            Column("Где найдено", lambda h: h.explanation if h.score else "", 190),
        ]

    # --- данные ---------------------------------------------------------------

    def use_sheet(self, sheet: Sheet) -> None:
        """Переиспользует уже загруженный каталог со страницы сопоставления."""
        if self.sheet is not None and self.sheet.path == sheet.path:
            return
        self.picker.field.setText(sheet.path)
        self.picker.set_status(f"{len(sheet.records)} строк · лист «{sheet.sheet_name}»", Palette.SUCCESS)
        # Снимок уже сделан страницей сопоставления — повторять его незачем.
        self._apply(sheet, snapshot=False)

    def _load(self, path: str) -> None:
        try:
            sheets = workbook.list_sheets(path)
        except Exception as error:  # noqa: BLE001
            self.notify(f"Не удалось открыть файл: {error}", ToastKind.ERROR)
            return
        self.picker.set_sheets(sheets, self.picker.sheet)
        self.settings.remember_file(path, source=True)
        self.picker.set_recent(self.settings.recent_source)
        self._reload()

    def _reload(self) -> None:
        if not self.picker.path:
            return
        self.picker.set_status("загрузка…", Palette.PRIMARY)
        run_task(
            workbook.load_sheet,
            self.picker.path,
            self.picker.sheet or None,
            role_overrides=self.settings.overrides_for(self.picker.path) or None,
            on_result=self._apply,
            on_error=lambda message: self.notify(f"Ошибка чтения: {message}", ToastKind.ERROR),
        )

    def _apply(self, sheet: Sheet, snapshot: bool = True) -> None:
        self.sheet = sheet
        self.engine.index(sheet)
        base = f"{len(sheet.records)} строк · лист «{sheet.sheet_name}»"
        self.picker.set_status(base, Palette.SUCCESS)
        if snapshot:
            snapshot_task.capture(
                sheet, self.settings, self.notify,
                status=lambda text: self.picker.set_status(
                    f"{base} · {text}" if text else base, Palette.SUCCESS))
        self._show_all()
        fade_in(self.table)

    # --- поиск ----------------------------------------------------------------

    def _search(self) -> None:
        if self.sheet is None:
            return
        query = self.query.text().strip()
        if not query:
            self._show_all()
            return
        self._hits = self.engine.search(query)
        self.table.set_items(self._hits)
        self.table.model_.set_terms(query.casefold().replace("ё", "е").split())
        self.summary.setText(
            f"Найдено {len(self._hits)} из {len(self.sheet.records)}"
            f" · лучший результат {self._hits[0].score:.0f}%" if self._hits
            else f"Ничего не найдено по запросу «{query}»")

    def _show_all(self) -> None:
        if self.sheet is None:
            return
        self.query.clear()
        self._hits = [SearchHit(record, 0.0, FieldRole.NAME, 0.0, []) for record in self.sheet.records]
        self.table.set_items(self._hits)
        self.table.model_.set_terms([])
        self.summary.setText(f"Показаны все записи: {len(self._hits)}")

    def focus_search(self) -> None:
        self.query.setFocus()
        self.query.selectAll()

    def _open_card(self, hit: object) -> None:
        if isinstance(hit, SearchHit):
            ProductCard(hit.record, self.sheet, self).exec()


def _number(value: object) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return -1.0
