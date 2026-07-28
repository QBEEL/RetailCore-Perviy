"""Поля ввода, которые не меняют значение от прокрутки страницы.

Колесо мыши над обычным QComboBox или QSpinBox меняет значение — молча и без
следов. В списке новинок так менялось количество, а в шапке заказа могла
поменяться колонка, куда записывается заказ. Здесь прокрутка передаётся
ближайшей прокручиваемой области, а значение меняется только осознанно:
вводом с клавиатуры, стрелками или кнопками поля.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QSpinBox,
    QWidget,
)


def _scrollable(widget: QWidget) -> QAbstractScrollArea | None:
    """Ближайшая прокручиваемая область: страница, таблица или список."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


def pass_wheel(widget: QWidget, event) -> None:  # type: ignore[no-untyped-def]
    """Прокручивает то, что под полем, вместо изменения значения.

    Событие отдаётся области напрямую: на всплытие проигнорированного события
    полагаться нельзя — до области оно доходит не во всех случаях.
    """
    area = _scrollable(widget)
    if area is not None and area.verticalScrollBar().maximum() > 0:
        QApplication.sendEvent(area.viewport(), event)
    else:
        event.ignore()


def click_focus_only(widget: QWidget) -> None:
    """Поле принимает фокус только от щелчка мыши по нему.

    Это свойство самого виджета, его проверяет Qt до всех обработчиков: ни
    колесо (по умолчанию у полей ввода политика WheelFocus, и фокус отдаётся
    ещё внутри QApplication::notify, до wheelEvent), ни Tab, ни наведение —
    что бы его ни порождало — фокус полю дать не могут. Ввод количества
    начинается только с осознанного щелчка; переход по Enter на следующую
    строку остаётся — программный setFocus политика не ограничивает.
    """
    widget.setFocusPolicy(Qt.FocusPolicy.ClickFocus)


class _SelectOnFocus:
    """Число выделяется только по щелчку левой кнопкой: ввод заменяет прежнее.

    Появление окна, Tab и программный setFocus значение не выделяют — иначе
    случайное нажатие клавиши стёрло бы уже введённое количество.
    """

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        click_focus_only(self)

    def focusInEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().focusInEvent(event)
        if (event.reason() != Qt.FocusReason.MouseFocusReason
                or not (QApplication.mouseButtons() & Qt.MouseButton.LeftButton)):
            # По Tab QAbstractSpinBox выделяет значение сам — снимаем.
            self.lineEdit().deselect()
            return
        # Щелчок ставит курсор уже после focusInEvent, поэтому выделение
        # восстанавливается следующим тиком цикла событий. Повторные щелчки
        # сюда не попадают: поле уже в фокусе, и курсор встаёт по месту щелчка.
        QTimer.singleShot(0, self.selectAll)

    def wheelEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        # Даже поле в фокусе не реагирует на колесо: иначе введённое количество
        # сбивалось бы при первой же прокрутке списка.
        pass_wheel(self, event)


class NumberInput(_SelectOnFocus, QSpinBox):
    """Целое число: количество, порог, лимит."""


class DecimalInput(_SelectOnFocus, QDoubleSpinBox):
    """Дробное число: вес поля, допуск."""


class SelectBox(QComboBox):
    """Выпадающий список, который прокрутка страницы не переключает."""

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        click_focus_only(self)

    def wheelEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        pass_wheel(self, event)
