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

# Как показывается вид различия: подпись и цвет строки. Рост цены для
# закупщика — плохая новость, снижение — хорошая, поэтому цвета разные.
_KINDS = {
    "added": ("+ новый", Palette.SUCCESS),
    "removed": ("− удалён", Palette.DANGER),
    "price_up": ("↑ цена выросла", Palette.DANGER),
    "price_down": ("↓ цена упала", Palette.SUCCESS),
    # Цена появилась или пропала: направление посчитать не от чего, и выдавать
    # это за подорожание нельзя.
    "price_set": ("~ цена появилась", Palette.PRIMARY),
    "price_gone": ("~ цены больше нет", Palette.WARNING),
    "changed": ("~ характеристики", Palette.PRIMARY),
}

# Порядок сортировки по умолчанию: сначала подорожания, они важнее всего.
_ORDER = {"price_up": 0, "price_down": 1, "price_gone": 2, "price_set": 3,
          "added": 4, "removed": 5, "changed": 6}


class _Row:
    """Строка таблицы различий."""

    __slots__ = ("kind", "name", "brand", "code", "before", "after", "delta", "sort")

    def __init__(self, kind: str, product: SnapshotProduct, before: str = "",
                 after: str = "", delta: str = "", sort: float = 0.0) -> None:
        self.kind = kind
        self.name = product.label
        self.brand = product.brand
        # У дистрибьютора артикулов может не быть вовсе — тогда товар
        # опознаётся по штрихкоду, и показывать нужно именно его.
        self.code = product.article or product.ean
        self.before = before
        self.after = after
        self.delta = delta
        self.sort = sort


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

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Фильтр по бренду, названию или штрихкоду…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda text: _filter(self.table, text))
        root.addWidget(self.search)

        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        self.table = DataTable([
            Column("Что", lambda r: _KINDS[r.kind][0], 145,
                   sort_key=lambda r: (_ORDER[r.kind], -abs(r.sort)),
                   color=lambda r: QColor(_KINDS[r.kind][1])),
            Column("Бренд", lambda r: r.brand, 130, highlight=True),
            Column("Наименование", lambda r: r.name, 280, highlight=True),
            Column("Артикул / штрихкод", lambda r: r.code, 150, highlight=True),
            Column("Было", lambda r: r.before, 90, align=right,
                   sort_key=lambda r: _number(r.before)),
            Column("Стало", lambda r: r.after, 90, align=right,
                   sort_key=lambda r: _number(r.after)),
            Column("Изменение", lambda r: r.delta, 100, align=right,
                   sort_key=lambda r: r.sort,
                   color=lambda r: QColor(_KINDS[r.kind][1]) if r.delta else None),
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
        if not result.total:
            self.summary.setText("Различий не найдено — версии совпадают.")
            return
        price = result.price_changes
        up = sum(1 for change in price if change.price_rose is True)
        down = sum(1 for change in price if change.price_rose is False)
        parts = [f"Новых: {len(result.added)}", f"удалённых: {len(result.removed)}",
                 f"подорожало: {up}", f"подешевело: {down}"]
        if other := len(price) - up - down:
            parts.append(f"цена появилась или пропала: {other}")
        parts.append(f"изменение характеристик: {len(result.changed) - len(price)}")
        self.summary.setText(" · ".join(parts))


def _rows(result: SnapshotDiff) -> list[_Row]:
    """Строки таблицы различий, отсортированные по важности для закупщика."""
    rows = [_Row("added", product, after=_money(product.price))
            for product in result.added]
    rows += [_Row("removed", product, before=_money(product.price))
             for product in result.removed]

    for change in result.changed:
        if change.price_changed:
            percent = change.price_percent
            before, after = _pair(change.before.price, change.after.price)
            rows.append(_Row(
                _price_kind(change), change.after, before=before, after=after,
                delta=f"{percent:+.1f}%" if percent is not None else "",
                sort=percent or 0.0))
        if other := [f for f in change.fields if f != "price"]:
            rows.append(_Row("changed", change.after,
                             before=_fields(change.before, other),
                             after=_fields(change.after, other)))

    # Сначала подорожания, внутри — по величине: это то, из-за чего вообще
    # открывают сравнение.
    rows.sort(key=lambda row: (_ORDER[row.kind], -abs(row.sort)))
    return rows


def _price_kind(change) -> str:  # type: ignore[no-untyped-def]
    """Направление изменения цены — только когда есть обе цены."""
    rose = change.price_rose
    if rose is not None:
        return "price_up" if rose else "price_down"
    return "price_gone" if change.after.price is None else "price_set"


def _fields(product: SnapshotProduct, names: Sequence[str]) -> str:
    return " · ".join(str(getattr(product, name, "")) for name in names)


def _pair(before: float | None, after: float | None) -> tuple[str, str]:
    """Обе цены одного вида: «4.30 → 4» читалось бы как разные величины."""
    fractional = any(value is not None and not float(value).is_integer()
                     for value in (before, after))
    return _money(before, fractional), _money(after, fractional)


def _money(value: float | None, fractional: bool | None = None) -> str:
    if value is None:
        return ""
    if fractional is None:
        fractional = not float(value).is_integer()
    digits = 2 if fractional else 0
    return f"{value:,.{digits}f}".replace(",", " ")


def _number(text: str) -> float:
    try:
        return float(text.replace(" ", "").replace(",", "."))
    except (AttributeError, ValueError):
        return -1.0


def _filter(table: DataTable, text: str) -> None:
    table.proxy.set_text(text)
    table.model_.set_terms(text.casefold().replace("ё", "е").split())
