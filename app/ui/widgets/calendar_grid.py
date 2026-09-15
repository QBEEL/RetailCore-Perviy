"""Сетка месяца: суммы и платежи по дням с цветовой индикацией.

Цвет показывает сумму за день, а просрочка — рамкой, не цветом. Иначе
просроченные тридцать тысяч потерялись бы в зелёном дне, а именно они требуют
внимания в первую очередь.

Пороги цвета берутся из настроек. Значения по умолчанию подобраны по истории:
медиана дня с оплатами — 1,43 млн, и шкала в сотнях тысяч покрасила бы красным
семь дней из десяти.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from PySide6.QtCore import QMimeData, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QFontMetrics, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QGridLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...core.payments import DEFAULT_DAY_LEVELS, Day, DayLevel, Payment, WEEKDAYS
from ..theme import Metrics, Palette

# Цвет уровня: рамка и фон. Зелёный не выделяется вовсе — спокойный день не
# должен притягивать взгляд.
LEVEL_COLORS: dict[DayLevel, tuple[str, str]] = {
    DayLevel.EMPTY: (Palette.BORDER, Palette.SURFACE),
    DayLevel.LIGHT: (Palette.SUCCESS, "#f0fdf4"),
    DayLevel.MEDIUM: (Palette.WARNING, "#fffbeb"),
    DayLevel.HIGH: ("#ea580c", "#fff7ed"),
    DayLevel.CRITICAL: (Palette.DANGER, "#fef2f2"),
}

MIME_PAYMENT = "application/x-retailcore-payment"

# Роли элемента списка дня: что переносим и можно ли это переносить вообще.
ROLE_PAYMENT_ID = Qt.ItemDataRole.UserRole
ROLE_MOVABLE = Qt.ItemDataRole.UserRole + 1


def payments_count(count: int) -> str:
    """«1 оплата», «2 оплаты», «5 оплат» — число со словом в нужном падеже."""
    tail, hundred = count % 10, count % 100
    if tail == 1 and hundred != 11:
        word = "оплата"
    elif 2 <= tail <= 4 and not 12 <= hundred <= 14:
        word = "оплаты"
    else:
        word = "оплат"
    return f"{count} {word}"


def payment_mime(ids: list[int]) -> QMimeData:
    """Пакует переносимые оплаты. Их всегда список: тянуть можно и несколько."""
    payload = QMimeData()
    payload.setData(MIME_PAYMENT, ",".join(str(value) for value in ids).encode())
    return payload


def payment_ids(payload: QMimeData) -> list[int]:
    """Разбирает список оплат из переноса. Чужой формат даёт пустой список."""
    if not payload.hasFormat(MIME_PAYMENT):
        return []
    raw = bytes(payload.data(MIME_PAYMENT)).decode(errors="ignore")
    return [int(part) for part in raw.split(",") if part.strip().lstrip("-").isdigit()]


def drag_badge(count: int, widget: QWidget) -> QPixmap:
    """Ярлык под курсором: сколько оплат тянем.

    Без подписи перенос одной оплаты и перенос всего дня выглядят одинаково, а
    отменить перенос нечем — только тащить обратно.
    """
    text = payments_count(count)
    metrics = QFontMetrics(widget.font())
    width = metrics.horizontalAdvance(text) + 22
    height = metrics.height() + 12
    ratio = widget.devicePixelRatioF()
    pixmap = QPixmap(int(width * ratio), int(height * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(Palette.PRIMARY))
    box = QRectF(0, 0, width, height)
    painter.drawRoundedRect(box, Metrics.RADIUS_SM, Metrics.RADIUS_SM)
    painter.setPen(QColor("#ffffff"))
    painter.setFont(widget.font())
    painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return pixmap


def money(value: float, *, short: bool = False) -> str:
    """Сумма для показа. Короткая форма — для тесных ячеек календаря."""
    if not value:
        return "—"
    if short:
        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:.1f} млн".replace(".", ",")
        if abs(value) >= 1_000:
            return f"{value / 1_000:.0f} тыс"
        return f"{value:.0f}"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


class DayCell(QFrame):
    """Одна клетка месяца."""

    clicked = Signal(object)
    payments_dropped = Signal(object, object, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.day: date | None = None
        self.data: Day | None = None
        self._style = ""
        self.setMinimumHeight(84)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        head = QLabel("", self)
        head.setStyleSheet("font-size: 13px; font-weight: 600;")
        self.number = head
        layout.addWidget(head)

        self.total = QLabel("", self)
        self.total.setStyleSheet("font-size: 12px; font-weight: 600;")
        layout.addWidget(self.total)

        self.detail = QLabel("", self)
        self.detail.setObjectName("Hint")
        self.detail.setStyleSheet(f"color: {Palette.TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(self.detail)
        layout.addStretch(1)

    def show_day(
        self,
        day: date | None,
        data: Day | None,
        *,
        levels: tuple[float, float, float] = DEFAULT_DAY_LEVELS,
        today: date | None = None,
        muted: bool = False,
    ) -> None:
        self.day = day
        self.data = data
        if day is None:
            self.number.setText("")
            self.total.setText("")
            self.detail.setText("")
            self._style = "background: transparent; border: none;"
            self.setStyleSheet(self._style)
            self.setToolTip("")
            return

        level = data.level(levels) if data is not None else DayLevel.EMPTY
        border, background = LEVEL_COLORS[level]
        weekend = day.weekday() >= 5
        if muted:
            background = Palette.SURFACE_ALT
            border = Palette.BORDER
        elif weekend and level is DayLevel.EMPTY:
            background = Palette.SURFACE_ALT

        width = 1
        if data is not None and data.overdue:
            # Просрочка помечается рамкой: цвет занят суммой, а пропустить
            # просроченный платёж нельзя даже в спокойный день.
            border, width = Palette.DANGER, 2
        elif today is not None and day == today:
            border, width = Palette.PRIMARY, 2

        self._style = (
            f"QFrame {{ background: {background}; border: {width}px solid {border};"
            f" border-radius: {Metrics.RADIUS_SM}px; }}")
        self.setStyleSheet(self._style)

        colour = Palette.TEXT_FAINT if muted else (
            Palette.TEXT_MUTED if weekend else Palette.TEXT)
        self.number.setText(str(day.day))
        self.number.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {colour};")

        if data is None or not data.count:
            self.total.setText("")
            self.detail.setText("")
            self.setToolTip("")
            return

        self.total.setText(money(data.total, short=True))
        self.total.setStyleSheet(
            f"font-size: 12px; font-weight: 600;"
            f" color: {Palette.TEXT_FAINT if muted else LEVEL_COLORS[level][0]};")
        names = [p.title for p in data.payments[:2]]
        if data.count > 2:
            names.append(f"ещё {data.count - 2}")
        self.detail.setText("\n".join(name[:22] for name in names))
        self.setToolTip(self._tooltip(data, level))

    def _tooltip(self, data: Day, level: DayLevel) -> str:
        lines = [
            f"{data.day:%d.%m.%Y} · {level.title}",
            f"платежей: {data.count} · сумма: {money(data.total)} ₽",
        ]
        if data.overdue:
            lines.append(f"просрочено: {data.overdue}")
        lines.append("")
        for payment in data.payments[:8]:
            lines.append(f"{money(payment.amount)} ₽ — {payment.title} ({payment.status.title})")
        if data.count > 8:
            lines.append(f"…ещё {data.count - 8}")
        return "\n".join(lines)

    # --- взаимодействие -------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self.day is not None and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.data if self.data is not None else Day(day=self.day))
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Перетаскивание клетки переносит весь день — неоплаченное из него.

        Отдельные оплаты выбираются в списке дня: в клетку помещаются два
        названия из десяти, и указать мышью на нужную строку в ней не на что.
        """
        if not (event.buttons() & Qt.MouseButton.LeftButton) or self.data is None:
            return
        movable = [p.id for p in self.data.payments if p.status.open]
        if not movable:
            return
        start = getattr(self, "_press", event.position().toPoint())
        moved = (event.position().toPoint() - start).manhattanLength()
        if moved < QApplication.startDragDistance():
            return
        drag = QDrag(self)
        drag.setMimeData(payment_mime(movable))
        drag.setPixmap(drag_badge(len(movable), self))
        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.day is None or not event.mimeData().hasFormat(MIME_PAYMENT):
            return
        event.acceptProposedAction()
        # Клетка под курсором подсвечивается: без неё в сетке 7×6 не понять,
        # на какое число попадёт перенос.
        self.setStyleSheet(
            f"QFrame {{ background: {Palette.SURFACE_ALT};"
            f" border: 2px dashed {Palette.PRIMARY};"
            f" border-radius: {Metrics.RADIUS_SM}px; }}")

    def dragLeaveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.setStyleSheet(self._style)

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.setStyleSheet(self._style)
        ids = payment_ids(event.mimeData())
        if self.day is None or not ids:
            return
        # День целиком тянут за клетку, выбранные оплаты — из списка дня.
        # Разница важна: перенос всего дня стоит подтвердить, выбранное — нет.
        self.payments_dropped.emit(ids, self.day, isinstance(event.source(), DayCell))
        event.acceptProposedAction()


class MonthGrid(QWidget):
    """Сетка 7×6 с днями месяца."""

    day_clicked = Signal(object)
    payments_moved = Signal(object, object, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.levels = DEFAULT_DAY_LEVELS
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        for column, name in enumerate(WEEKDAYS):
            label = QLabel(name, self)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            colour = Palette.TEXT_FAINT if column >= 5 else Palette.TEXT_MUTED
            label.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {colour};")
            layout.addWidget(label, 0, column)

        self.cells: list[DayCell] = []
        for index in range(42):
            cell = DayCell(self)
            cell.clicked.connect(self.day_clicked.emit)
            cell.payments_dropped.connect(self.payments_moved.emit)
            layout.addWidget(cell, 1 + index // 7, index % 7)
            self.cells.append(cell)
        for column in range(7):
            layout.setColumnStretch(column, 1)
        for row in range(1, 7):
            layout.setRowStretch(row, 1)

    def show_month(
        self,
        year: int,
        month: int,
        days: dict[date, Day],
        *,
        levels: tuple[float, float, float] | None = None,
        today: date | None = None,
    ) -> None:
        """Раскладывает месяц. Дни соседних месяцев показываются приглушённо."""
        if levels is not None:
            self.levels = levels
        moment = today or date.today()
        first = date(year, month, 1)
        # Сетка начинается с понедельника недели, в которую попало первое число.
        start = first - timedelta(days=first.weekday())
        for index, cell in enumerate(self.cells):
            day = start + timedelta(days=index)
            cell.show_day(
                day,
                days.get(day),
                levels=self.levels,
                today=moment,
                muted=day.month != month,
            )

    @staticmethod
    def month_range(year: int, month: int) -> tuple[date, date]:
        last = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last)


class DayPaymentList(QListWidget):
    """Оплаты выбранного дня. Отсюда их переносят на другую дату.

    Выделение множественное: на один день приходится и десять оплат, а
    переносят обычно часть из них — например, всё по одному поставщику.
    Неоплаченные строки не тянутся вовсе, статус «Оплачено» датой не двигают.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    @staticmethod
    def mark(item: QListWidgetItem, payment: Payment) -> QListWidgetItem:
        """Привязывает оплату к строке списка."""
        item.setData(ROLE_PAYMENT_ID, payment.id)
        item.setData(ROLE_MOVABLE, payment.status.open)
        return item

    def selected_ids(self, *, movable_only: bool = True) -> list[int]:
        return [
            int(item.data(ROLE_PAYMENT_ID) or 0)
            for item in self.selectedItems()
            if item.data(ROLE_PAYMENT_ID) and (not movable_only or item.data(ROLE_MOVABLE))
        ]

    def startDrag(self, actions) -> None:  # type: ignore[no-untyped-def]
        """Тянем выделенное. Строки списка не трогаем — дату меняет страница."""
        ids = self.selected_ids()
        if not ids:
            return
        drag = QDrag(self)
        drag.setMimeData(payment_mime(ids))
        drag.setPixmap(drag_badge(len(ids), self))
        drag.exec(Qt.DropAction.MoveAction)


class LevelLegend(QWidget):
    """Подпись к цветовой шкале — иначе цвета приходится угадывать."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(10)
        self._labels: list[QLabel] = []
        for index in range(4):
            label = QLabel("", self)
            label.setStyleSheet("font-size: 11px;")
            self._layout.addWidget(label, 0, index)
            self._labels.append(label)
        self._layout.setColumnStretch(4, 1)

    def show_levels(self, levels: tuple[float, float, float]) -> None:
        low, middle, high = levels
        captions = [
            (DayLevel.LIGHT, f"до {money(low, short=True)}"),
            (DayLevel.MEDIUM, f"до {money(middle, short=True)}"),
            (DayLevel.HIGH, f"до {money(high, short=True)}"),
            (DayLevel.CRITICAL, f"свыше {money(high, short=True)}"),
        ]
        for label, (level, caption) in zip(self._labels, captions):
            colour = LEVEL_COLORS[level][0]
            label.setText(f"● {caption}")
            label.setStyleSheet(f"font-size: 11px; color: {colour}; font-weight: 600;")
