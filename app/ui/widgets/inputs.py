"""Поля ввода, которые не меняют значение от прокрутки страницы.

Колесо мыши над обычным QComboBox или QSpinBox меняет значение — молча и без
следов. В списке новинок так менялось количество, а в шапке заказа могла
поменяться колонка, куда записывается заказ. Здесь прокрутка передаётся
ближайшей прокручиваемой области, а значение меняется только осознанно:
вводом с клавиатуры, стрелками или кнопками поля.
"""
from __future__ import annotations

from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QComboBox,
    QCompleter,
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


class _WordsFilter(QSortFilterProxyModel):
    """Оставляет строки, где встречается каждое набранное слово — в любом порядке.

    Стандартный QCompleter ищет подстроку целиком, и «поставщику бьюти» не
    нашло бы «Оплата поставщику (бьюти)»: между словами стоит скобка.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._words: list[str] = []

    def set_text(self, text: str) -> None:
        self._words = _fold(text).split()
        self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent) -> bool:  # type: ignore[no-untyped-def]
        title = _fold(str(self.sourceModel().index(row, 0, parent).data() or ""))
        return all(word in title for word in self._words)


def _fold(text: str) -> str:
    """Регистр и «ё» не мешают поиску: «Ремонт» находится по «ремонт», «ёлка» по «елка»."""
    return text.casefold().replace("ё", "е")


class SearchSelect(SelectBox):
    """Выбор из длинного списка с поиском: набираешь слова — список сужается.

    Сохранить можно только то, что есть в списке: `value()` отдаёт название из
    списка или пустую строку, а набранное мимо списка видно по
    `has_unmatched_text()`. Иначе в оплату попала бы «статья», которой нет ни в
    справочнике, ни в отчётах.
    """

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._words = _WordsFilter(self)
        self._words.setSourceModel(self.model())
        completer = QCompleter(self._words, self)
        # Отбор делает свой фильтр: встроенный проверяет только начало строки.
        completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        completer.setMaxVisibleItems(12)
        self.setCompleter(completer)
        # Названия длиннее поля: список шире него, иначе вместо отличий в конце
        # названия («бьюти» и «фэшн») виден один и тот же обрезанный хвост.
        completer.popup().setMinimumWidth(560)
        self.view().setMinimumWidth(560)
        self.lineEdit().setPlaceholderText("Начните вводить для поиска")
        self.lineEdit().textEdited.connect(self._words.set_text)
        completer.activated[str].connect(self._chosen)
        # Ушли из поля с недописанным текстом — возвращаем выбранное: в поле
        # должно стоять то, что действует.
        self.lineEdit().editingFinished.connect(self._restore)
        self.activated.connect(lambda _: self._words.set_text(""))

    def set_items(self, titles: list[str], current: str = "") -> None:
        """Заменяет список. Текущая статья, которой в списке нет, остаётся:
        оплату со старой статьёй нельзя открыть и молча лишить её."""
        titles = list(titles)
        if current and current not in titles:
            titles.insert(0, current)
        self.clear()
        self.addItems(titles)
        for row, title in enumerate(titles):
            self.setItemData(row, title, Qt.ItemDataRole.ToolTipRole)
        self.set_value(current)

    def set_value(self, title: str) -> None:
        index = self.findText(title, Qt.MatchFlag.MatchFixedString)
        if index >= 0:
            self.setCurrentIndex(index)
        else:
            self.setCurrentIndex(-1)
            self.lineEdit().clear()
        self._words.set_text("")

    def value(self) -> str:
        """Выбранное название; пусто, если ничего не выбрано."""
        index = self.findText(self.currentText(), Qt.MatchFlag.MatchFixedString)
        return self.itemText(index) if index >= 0 else ""

    def has_unmatched_text(self) -> bool:
        """В поле набрано что-то, чего нет в списке."""
        return bool(self.currentText().strip()) and not self.value()

    def _chosen(self, title: str) -> None:
        self.set_value(title)

    def _restore(self) -> None:
        if self.has_unmatched_text() and not self.completer().popup().isVisible():
            index = self.currentIndex()
            self.setEditText(self.itemText(index) if index >= 0 else "")
