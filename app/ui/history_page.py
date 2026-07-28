"""История данных: список сохранённых снимков каталогов и работа с ними."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import snapshots
from ..core.settings import AppSettings
from ..core.snapshots import Snapshot
from . import icons
from .tasks import run_task
from .theme import Metrics, Palette
from .widgets.common import Card, Hint, SectionTitle, Subtitle, Title, fade_in
from .widgets.snapshot_dialogs import SnapshotCompareDialog, SnapshotViewDialog
from .widgets.table import Column, DataTable
from .widgets.toast import ToastKind


class HistoryPage(QWidget):
    """Каждая загрузка каталога сохраняется здесь как отдельная версия."""

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, ToastKind], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._items: list[Snapshot] = []
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Title("История данных", self))
        root.addWidget(Subtitle(
            "Каждая загрузка каталога сохраняется целиком. Состояние на любую дату "
            "можно открыть, а две версии — сравнить между собой.", self))

        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Снимки выгрузок", card))
        header.addStretch(1)

        self.open_button = self._action(card, "Открыть", "card", self.open_selected)
        self.compare_button = self._action(card, "Сравнить версии", "compare", self.compare)
        self.delete_button = self._action(card, "Удалить", "trash", self.delete_selected)
        self.delete_button.setObjectName("Danger")
        refresh = self._action(card, "Обновить", "reset", self.reload)
        for button in (self.open_button, self.compare_button, self.delete_button, refresh):
            header.addWidget(button)
        body.addLayout(header)

        self.summary = Hint("", card)
        body.addWidget(self.summary)

        self.table = DataTable(self._columns(), card)
        self.table.item_activated.connect(lambda item: self._open(item))
        self.table.selectionModel().selectionChanged.connect(self._update_actions)
        body.addWidget(self.table, 1)
        root.addWidget(card, 1)

        self._update_actions()

    def _action(self, parent: QWidget, title: str, icon: str, handler) -> QPushButton:
        button = QPushButton(title, parent)
        button.setIcon(icons.icon(icon))
        button.clicked.connect(handler)
        return button

    def _columns(self) -> list[Column]:
        return [
            Column("Дата", lambda s: f"{s.created_at:%d.%m.%y %H:%M}", 120,
                   sort_key=lambda s: s.created_at),
            Column("Файл", lambda s: s.source_file_name, 280, highlight=True),
            Column("Лист", lambda s: s.sheet_name, 120),
            Column("Товаров", lambda s: s.total_products, 90,
                   sort_key=lambda s: s.total_products,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            Column("Бренд", lambda s: s.brand, 130),
            Column("Категория", lambda s: s.category, 170),
            Column("Пользователь", lambda s: s.user_id, 130),
        ]

    # --- данные ---------------------------------------------------------------

    def reload(self) -> None:
        """Читает список снимков в фоне: база может быть на сетевом диске."""
        run_task(
            snapshots.list_snapshots,
            on_result=self._apply,
            on_error=lambda message: self.notify(f"Не удалось прочитать историю: {message}",
                                                 ToastKind.ERROR),
        )

    def _apply(self, items: list[Snapshot]) -> None:
        self._items = items
        self.table.set_items(items)
        self._update_actions()
        if items:
            size = snapshots.database_size() / 1024 / 1024
            products = sum(s.total_products for s in items)
            self.summary.setText(
                f"Снимков: {len(items)} · товаров всего: {products} · база: {size:.1f} МБ")
        else:
            self.summary.setText(
                "История пуста. Загрузите каталог на странице «Сопоставление» или «Каталог» — "
                "снимок создастся автоматически.")
        fade_in(self.table)

    def _update_actions(self) -> None:
        selected = self.table.current_item() is not None
        self.open_button.setEnabled(selected)
        self.delete_button.setEnabled(selected)
        self.compare_button.setEnabled(len(self._items) > 1)

    # --- действия ---------------------------------------------------------------

    def open_selected(self) -> None:
        if isinstance(snapshot := self.table.current_item(), Snapshot):
            self._open(snapshot)

    def _open(self, snapshot: object) -> None:
        if not isinstance(snapshot, Snapshot):
            return
        run_task(
            snapshots.products,
            snapshot.id,
            on_result=lambda products: SnapshotViewDialog(snapshot, products, self).exec(),
            on_error=lambda message: self.notify(f"Не удалось открыть снимок: {message}",
                                                 ToastKind.ERROR),
        )

    def compare(self) -> None:
        if len(self._items) < 2:
            self.notify("Для сравнения нужно хотя бы два снимка", ToastKind.WARNING)
            return
        SnapshotCompareDialog(self._items, snapshots.products, self).exec()

    def delete_selected(self) -> None:
        snapshot = self.table.current_item()
        if not isinstance(snapshot, Snapshot):
            return
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Удалить снимок")
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setText(f"Удалить снимок от {snapshot.created_at:%d.%m.%Y %H:%M}?")
        confirm.setInformativeText(
            f"{snapshot.source_file_name} · {snapshot.total_products} товаров.\n"
            "Эти данные больше не получится сравнить с другими версиями.")
        yes = confirm.addButton("Удалить", QMessageBox.ButtonRole.DestructiveRole)
        confirm.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        confirm.exec()
        if confirm.clickedButton() is not yes:
            return

        run_task(
            snapshots.delete,
            snapshot.id,
            on_result=lambda _: self._on_deleted(snapshot),
            on_error=lambda message: self.notify(f"Не удалось удалить снимок: {message}",
                                                 ToastKind.ERROR),
        )

    def _on_deleted(self, snapshot: Snapshot) -> None:
        self.notify(f"Снимок от {snapshot.created_at:%d.%m.%Y} удалён", ToastKind.SUCCESS)
        self.reload()
