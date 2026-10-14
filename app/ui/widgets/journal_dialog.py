"""Окно записи журнала: полная картина одного действия и история той же оплаты.

Строка таблицы журнала режет изменения по ширине колонки и показывает одно
действие. Здесь — всё без сокращений: кто, когда, что было и что стало, чем
это выглядит для проверяющего и что происходило с той же оплатой до и после.
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.payments import admin
from ...core.payments.admin import Entry
from .. import icons
from ..tasks import run_task
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle
from .table import Column, DataTable


def _moment(value: datetime | None) -> str:
    return f"{value:%d.%m.%Y %H:%M:%S}" if value else ""


class JournalEntryDialog(QDialog):
    """Одна запись журнала со всеми подробностями."""

    def __init__(self, entry: Entry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entry = entry
        self.history: list[Entry] = []
        self.setWindowTitle("Запись журнала")
        self.setModal(True)
        self.setMinimumSize(880, 600)
        self._build()
        if entry.is_payment and entry.entity_id:
            self._load_history()
        else:
            self.history_title.setVisible(False)
            self.history_table.setVisible(False)
            self.history_hint.setVisible(False)

    # --- разметка -----------------------------------------------------------------

    def _build(self) -> None:
        entry = self.entry
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle(f"{entry.entity.capitalize()} · {entry.action}"))

        info = QGridLayout()
        info.setHorizontalSpacing(Metrics.GAP)
        info.setVerticalSpacing(4)
        info.setColumnStretch(1, 1)
        for row, (caption, value) in enumerate((
                ("Когда", _moment(entry.at)),
                ("Кто", entry.user),
                ("Запись", entry.record or "—"))):
            label = QLabel(caption, self)
            label.setObjectName("FormCaption")
            info.addWidget(label, row, 0)
            shown = QLabel(value, self)
            shown.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            shown.setWordWrap(True)
            info.addWidget(shown, row, 1)
        root.addLayout(info)

        if flags := entry.flags:
            warning = Hint("Стоит проверить: " + "; ".join(flags) + ".")
            warning.setStyleSheet(f"color: {Palette.WARNING}; font-weight: 600;")
            root.addWidget(warning)

        root.addWidget(SectionTitle("Что изменилось"))
        self.changes_table = DataTable([
            Column("Поле", lambda r: r[0], 170),
            Column("Было", lambda r: r[1], 250),
            Column("Стало", lambda r: r[2], 300),
        ], self)
        rows = entry.rows()
        self.changes_table.set_items(rows)
        self.changes_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Высота по строкам: пока их немного, таблица не должна ни прятать их за
        # полосой прокрутки, ни занимать место пустотой.
        header = self.changes_table.horizontalHeader().sizeHint().height()
        self.changes_table.setFixedHeight(
            min(header + Metrics.ROW_HEIGHT * max(len(rows), 1) + 6, 300))
        root.addWidget(self.changes_table)
        if not rows:
            root.addWidget(Hint("Подробностей нет: действие записано без изменённых полей."))

        self.history_title = SectionTitle("История этой оплаты")
        root.addWidget(self.history_title)
        self.history_hint = Hint("Загружаю…")
        root.addWidget(self.history_hint)
        self.history_table = DataTable([
            Column("Когда", lambda e: _moment(e.at), 140,
                   sort_key=lambda e: e.at or datetime.min),
            Column("Кто", lambda e: e.user, 140),
            Column("Действие", lambda e: e.action, 130),
            Column("Изменения", lambda e: e.summary, 300),
        ], self)
        self.history_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.history_table.model_.set_row_tint(self._tint)
        root.addWidget(self.history_table, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Закрыть", self)
        close.setIcon(icons.icon("close"))
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        root.addLayout(buttons)

    def _tint(self, item: Entry) -> QColor | None:
        """Открытое действие в общей истории — выделено."""
        return QColor(Palette.PRIMARY_SOFT) if item.id == self.entry.id else None

    # --- история ------------------------------------------------------------------

    def _load_history(self) -> None:
        run_task(_history, self.entry.entity_id,
                 on_result=self._show_history, on_error=self._history_failed)

    def _show_history(self, entries: list[Entry]) -> None:
        self.history = entries
        self.history_table.set_items(entries)
        self.history_hint.setText(
            f"Действий с этой оплатой: {len(entries)}. Открытое выделено. "
            "Массовые правки сюда не входят."
            if entries else "Других действий с этой оплатой нет.")

    def _history_failed(self, message: str) -> None:
        self.history_hint.setText(f"Историю загрузить не удалось: {message}")


def _history(payment_id: int) -> list[Entry]:
    """Все действия с оплатой, свежие сверху.

    Отбор повторяется здесь: сервер прежней версии условия по номеру не знает и
    вернул бы весь журнал, а в чужих оплатах человеку разбираться незачем.
    Массовые правки (номер 0) сюда не попадают — у них нет своей оплаты.
    """
    found = admin.journal(limit=500, entity="payment", record_id=payment_id)
    return [e for e in found if e.is_payment and e.entity_id == payment_id]
