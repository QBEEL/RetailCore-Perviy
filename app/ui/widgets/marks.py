"""Цветная пометка оплаты: кружок, подсветка строки и цвет подписи.

Цвет задаёт администратор статье ДДС (`core.payments.dds`), а здесь он только
превращается в то, что рисуют таблица и календарь. Все три производные — кружок,
подсветка и цвет текста — считаются от одного «#RRGGBB», чтобы оплата выглядела
одним цветом везде.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

from ...core.payments import dds

DOT_SIZE = 8

_icons: dict[str, QIcon] = {}


def color_of(title: str) -> QColor | None:
    """Цвет статьи или None, если статью не красили."""
    item = dds.mark_of(title)
    if item is None or not item.color:
        return None
    color = QColor(item.color)
    return color if color.isValid() else None


def note_of(title: str) -> str:
    """Метка статьи («Маркетинг») или пустая строка."""
    item = dds.mark_of(title)
    return item.note if item else ""


def blank_icon() -> QIcon:
    """Пустое место размером с кружок — чтобы названия в таблице стояли ровно."""
    if "" not in _icons:
        pixmap = QPixmap(DOT_SIZE, DOT_SIZE)
        pixmap.fill(Qt.GlobalColor.transparent)
        _icons[""] = QIcon(pixmap)
    return _icons[""]


def has_marks() -> bool:
    """Есть ли в справочнике хоть одна окрашенная статья."""
    return any(item.color for item in dds._marks.values())


def dot_icon(color: QColor) -> QIcon:
    """Цветной кружок. Рисуется один раз на цвет."""
    key = color.name()
    if key not in _icons:
        scale = 2  # рисуем вдвое крупнее: на экранах с масштабом край не рябит
        pixmap = QPixmap(DOT_SIZE * scale, DOT_SIZE * scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        margin = scale
        painter.drawEllipse(QRectF(margin, margin, pixmap.width() - 2 * margin,
                                   pixmap.height() - 2 * margin))
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        _icons[key] = QIcon(pixmap)
    return _icons[key]


def tint(color: QColor) -> QColor:
    """Подсветка строки: тот же цвет, но прозрачный — текст остаётся читаемым."""
    soft = QColor(color)
    soft.setAlpha(46)
    return soft


def ink(color: QColor) -> QColor:
    """Цвет подписи: темнее кружка, чтобы читаться на подсвеченной строке."""
    return color.darker(135)
