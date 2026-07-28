"""Карточка товара: все поля записи в читаемом виде."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core.models import FieldRole, Record, Sheet
from .. import icons
from ..theme import Metrics, Palette
from .common import Badge, Divider, apply_shadow

# Порядок вывода полей в карточке: идентификаторы сверху, описания ниже.
_ORDER = (
    FieldRole.ARTICLE, FieldRole.SKU, FieldRole.EAN, FieldRole.BRAND, FieldRole.CATEGORY,
    FieldRole.VOLUME, FieldRole.COLOR, FieldRole.SIZE, FieldRole.PRICE, FieldRole.QUANTITY,
    FieldRole.MANUFACTURER, FieldRole.DATE, FieldRole.DESCRIPTION, FieldRole.NOTE,
    FieldRole.NAME_ALT,
)


class ProductCard(QDialog):
    """Модальная карточка с плавным появлением."""

    def __init__(self, record: Record, sheet: Sheet | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Карточка товара")
        self.setMinimumWidth(560)
        self.setMaximumHeight(760)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 4, Metrics.PAD + 4, Metrics.PAD + 4, Metrics.PAD + 4)
        root.setSpacing(Metrics.GAP)

        title = QLabel(record.label, self)
        title.setObjectName("PageTitle")
        title.setWordWrap(True)
        root.addWidget(title)

        chips = QHBoxLayout()
        chips.setSpacing(7)
        if record.quantity:
            chips.addWidget(Badge(str(record.quantity), Palette.INFO, Palette.INFO_SOFT, self))
        if article := record.text(FieldRole.ARTICLE):
            chips.addWidget(Badge(article, Palette.PRIMARY, Palette.PRIMARY_SOFT, self))
        chips.addWidget(Badge(f"Строка {record.row}", Palette.TEXT_MUTED, Palette.SURFACE_ALT, self))
        chips.addStretch(1)
        root.addLayout(chips)
        root.addWidget(Divider(self))

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget(scroll)
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 4, 0, 4)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(9)
        grid.setColumnStretch(1, 1)

        for row, (label, value) in enumerate(_rows(record, sheet)):
            name = QLabel(label, content)
            name.setStyleSheet(f"color: {Palette.TEXT_MUTED};")
            name.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            name.setMinimumWidth(150)
            data = QLabel(value, content)
            data.setWordWrap(True)
            data.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            data.setStyleSheet("font-weight: 500;")
            grid.addWidget(name, row, 0)
            grid.addWidget(data, row, 1)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Закрыть", self)
        close.setObjectName("Primary")
        close.setIcon(icons.icon("check", Palette.TEXT_ON_PRIMARY))
        close.clicked.connect(self.accept)
        close.setDefault(True)
        buttons.addWidget(close)
        root.addLayout(buttons)

        apply_shadow(self)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._animation = QPropertyAnimation(self, b"windowOpacity", self)
        self._animation.setDuration(180)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.start()


def _rows(record: Record, sheet: Sheet | None) -> list[tuple[str, str]]:
    """Сначала известные роли в заданном порядке, затем прочие колонки файла."""
    rows: list[tuple[str, str]] = []
    for role in _ORDER:
        if value := record.text(role):
            rows.append((role.title, value))
    if sheet is None:
        return rows
    shown = {FieldRole.NAME, *_ORDER}
    for column in sheet.columns:
        if column.role in shown or column.index >= len(record.values):
            continue
        value = record.values[column.index]
        if value not in (None, ""):
            rows.append((column.title, str(value)))
    return rows
