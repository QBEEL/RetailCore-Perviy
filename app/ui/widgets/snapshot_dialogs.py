"""Просмотр снимка и сравнение двух версий каталога."""
from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.snapshots import Snapshot, SnapshotDiff, SnapshotProduct, diff
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle
from .inputs import SelectBox
from .table import Column, DataTable

# Как показывается вид различия: подпись и цвет строки.
_KINDS = {
    "added": ("+ новый", Palette.SUCCESS),
    "removed": ("− удалён", Palette.DANGER),
    "price": ("↑ цена", Palette.WARNING),
    "changed": ("↑ характеристики", Palette.PRIMARY),
}


class SnapshotViewDialog(QDialog):
    """Состояние каталога на момент загрузки — только чтение."""

    def __init__(self, snapshot: Snapshot, products: Sequence[SnapshotProduct],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Снимок от {snapshot.created_at:%d.%m.%Y %H:%M}")
        self.resize(1000, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle(snapshot.source_file_name, self))
        root.addWidget(Hint(
            f"Загружен {snapshot.created_at:%d.%m.%Y в %H:%M} · лист «{snapshot.sheet_name}» · "
            f"товаров {snapshot.total_products}"
            + (f" · бренд {snapshot.brand}" if snapshot.brand else "")
            + (f" · пользователь {snapshot.user_id}" if snapshot.user_id else ""), self))

        search = QLineEdit(self)
        search.setPlaceholderText("Поиск по снимку…")
        search.setClearButtonEnabled(True)
        root.addWidget(search)

        table = DataTable([
            Column("Строка", lambda p: p.row, 62,
                   align=Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter),
            Column("Наименование", lambda p: p.name, 320, highlight=True),
            Column("Артикул", lambda p: p.article, 150, highlight=True),
            Column("Штрихкод", lambda p: p.ean, 135, highlight=True),
            Column("Объём", lambda p: p.volume, 90),
            Column("Цена", lambda p: _money(p.price), 90,
                   sort_key=lambda p: p.price if p.price is not None else -1,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            Column("Бренд", lambda p: p.brand, 120),
            Column("Категория", lambda p: p.category, 150),
        ], self)
        table.set_items(list(products))
        search.textChanged.connect(lambda text: _filter(table, text))
        root.addWidget(table, 1)

        close = QPushButton("Закрыть", self)
        close.setAutoDefault(False)
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close)
        root.addLayout(buttons)


class SnapshotCompareDialog(QDialog):
    """Выбор двух снимков и разбор различий между ними."""

    def __init__(self, snapshots: Sequence[Snapshot], loader, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._snapshots = list(snapshots)
        self._loader = loader          # callable(snapshot_id) -> list[SnapshotProduct]
        self.setWindowTitle("Сравнение версий")
        self.resize(1040, 640)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle("Что изменилось между версиями", self))

        choose = QHBoxLayout()
        choose.setSpacing(9)
        self.before = SelectBox(self)
        self.after = SelectBox(self)
        for box in (self.before, self.after):
            for snapshot in self._snapshots:
                box.addItem(snapshot.label, snapshot.id)
        # По умолчанию сравниваются две последние версии: снимки идут от новых
        # к старым, поэтому «было» — второй сверху, «стало» — самый свежий.
        if len(self._snapshots) > 1:
            self.before.setCurrentIndex(1)
        choose.addWidget(Hint("Было:", self))
        choose.addWidget(self.before, 1)
        choose.addWidget(Hint("Стало:", self))
        choose.addWidget(self.after, 1)

        run = QPushButton("Сравнить", self)
        run.setObjectName("Primary")
        run.setAutoDefault(False)
        run.clicked.connect(self._compare)
        choose.addWidget(run)
        root.addLayout(choose)

        self.summary = Hint("Выберите версии и нажмите «Сравнить».", self)
        root.addWidget(self.summary)

        self.table = DataTable([
            Column("Что", lambda r: _KINDS[r[0]][0], 150,
                   color=lambda r: QColor(_KINDS[r[0]][1])),
            Column("Наименование", lambda r: r[1], 320),
            Column("Артикул", lambda r: r[2], 150),
            Column("Было", lambda r: r[3], 150,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            Column("Стало", lambda r: r[4], 150,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
        ], self)
        root.addWidget(self.table, 1)

        close = QPushButton("Закрыть", self)
        close.setAutoDefault(False)
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close)
        root.addLayout(buttons)

        if len(self._snapshots) > 1:
            self._compare()

    def _compare(self) -> None:
        before_id, after_id = self.before.currentData(), self.after.currentData()
        if before_id == after_id:
            self.summary.setText("Выбрана одна и та же версия — сравнивать нечего.")
            self.table.set_items([])
            return
        result = diff(self._loader(before_id), self._loader(after_id))
        self.table.set_items(_rows(result))
        self.summary.setText(
            f"Новых: {len(result.added)} · удалённых: {len(result.removed)} · "
            f"изменение цены: {len(result.price_changes)} · "
            f"изменение характеристик: {len(result.changed) - len(result.price_changes)}"
            if result.total else "Различий не найдено — версии совпадают.")


def _rows(result: SnapshotDiff) -> list[tuple[str, str, str, str, str]]:
    """Плоские строки таблицы: вид различия, товар и значения до/после."""
    rows: list[tuple[str, str, str, str, str]] = [
        ("added", product.label, product.article, "", _money(product.price))
        for product in result.added
    ]
    rows += [("removed", product.label, product.article, _money(product.price), "")
             for product in result.removed]
    for change in result.changed:
        if change.price_changed:
            rows.append(("price", change.after.label, change.after.article,
                         _money(change.before.price), _money(change.after.price)))
        if other := [f for f in change.fields if f != "price"]:
            rows.append(("changed", change.after.label, change.after.article,
                         _fields(change.before, other), _fields(change.after, other)))
    return rows


def _fields(product: SnapshotProduct, names: Sequence[str]) -> str:
    return " · ".join(str(getattr(product, name, "")) for name in names)


def _money(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.2f}"


def _filter(table: DataTable, text: str) -> None:
    table.proxy.set_text(text)
    table.model_.set_terms(text.casefold().replace("ё", "е").split())
