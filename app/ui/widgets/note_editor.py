"""Редактор заметок: форматируемый текст, который хранится как Markdown.

Qt умеет читать и писать Markdown сам (`setMarkdown`/`toMarkdown`), поэтому
хранилищу достаточно строки. Сложность в другом: обычным абзацем, заголовком,
цитатой и кодом блок делают не отдельные «стили», а набор свойств блока и текста
внутри него, причём разный в разных версиях Qt. Чтобы не подбирать их вручную,
образцы берутся у самого Qt — читаем крошечный Markdown и копируем то, что он
выставил. Обновление Qt ничего не сломает: образец обновится вместе с ним.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Callable

from PySide6.QtCore import QEvent, QMimeData, QRectF, QUrl, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QTextBlock,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextListFormat,
)
from PySide6.QtWidgets import QHBoxLayout, QInputDialog, QTextEdit, QToolButton, QWidget

from .. import icons
from ..theme import Palette

# Виды блока. Список — отдельные виды, а не свойство: в Markdown у пункта либо
# точка, либо номер, либо флажок.
PLAIN, H1, H2, H3, QUOTE, CODE, BULLET, NUMBER, CHECK = (
    "plain", "h1", "h2", "h3", "quote", "code", "bullet", "number", "check")

_HEADINGS = {H1: 1, H2: 2, H3: 3}
_SAMPLES = {
    H1: "# x", H2: "## x", H3: "### x",
    QUOTE: "> x", CODE: "```\nx\n```",
}


@lru_cache(maxsize=None)
def _sample(kind: str) -> tuple[QTextBlockFormat, QTextCharFormat]:
    """Формат блока и текста, которым Qt оформляет этот вид блока при чтении Markdown."""
    document = QTextDocument()
    document.setMarkdown(_SAMPLES[kind])
    block = document.firstBlock()
    fragments = block.begin()
    text_format = fragments.fragment().charFormat() if not fragments.atEnd() else QTextCharFormat()
    return QTextBlockFormat(block.blockFormat()), QTextCharFormat(text_format)


@lru_cache(maxsize=1)
def _inline_code_format() -> QTextCharFormat:
    """Формат кода внутри строки — тот, что Qt ставит на `текст` в обратных кавычках."""
    document = QTextDocument()
    document.setMarkdown("a `b` c")
    fragments = document.firstBlock().begin()
    while not fragments.atEnd():
        fmt = fragments.fragment().charFormat()
        if fmt.fontFixedPitch():
            return QTextCharFormat(fmt)
        fragments += 1
    return QTextCharFormat()


# Свойства текста, которыми Qt отличает код от обычного текста.
_CODE_TEXT_PROPERTIES = (
    QTextFormat.Property.FontFamilies, QTextFormat.Property.FontFamily,
    QTextFormat.Property.FontFixedPitch, QTextFormat.Property.FontPointSize,
)
# Цитата и код различаются фоном; у цитаты ещё полоса слева (см. paintEvent).
_CODE_BACKGROUND = "#f1f5f9"
_QUOTE_BACKGROUND = Palette.PRIMARY_SOFT
_QUOTE_BAR = Palette.PRIMARY
_QUOTE_BAR_WIDTH = 3
_LINE_BREAK = " "


@lru_cache(maxsize=1)
def _rule_format() -> QTextBlockFormat:
    """Блок-разделитель (горизонтальная черта) — он пустой, вся суть в его формате."""
    document = QTextDocument()
    document.setMarkdown("a\n\n---\n\nb")
    return QTextBlockFormat(document.findBlockByNumber(1).blockFormat())


def kind_of(block: QTextBlock) -> str:
    """Вид блока. Метка флажка у обычного абзаца — остаток разбора, её не считаем."""
    block_format = block.blockFormat()
    level = block_format.headingLevel()
    if level in (1, 2, 3):
        return {1: H1, 2: H2, 3: H3}[level]
    items = block.textList()
    if items is not None:
        if block_format.marker() != QTextBlockFormat.MarkerType.NoMarker:
            return CHECK
        if items.format().style() == QTextListFormat.Style.ListDecimal:
            return NUMBER
        return BULLET
    if block_format.hasProperty(QTextFormat.Property.BlockCodeFence):
        return CODE
    if block_format.hasProperty(QTextFormat.Property.BlockQuoteLevel):
        return QUOTE
    return PLAIN


class NoteEditor(QTextEdit):
    """Текст страницы. Сочетания Ctrl+B и Ctrl+I ловит сам, мимо оконных действий."""

    state_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Вставка из браузера или Excel приносит чужие шрифты и цвета. Простым
        # текстом результат предсказуем, а нужное оформление ставится панелью.
        self.setAcceptRichText(False)
        self.setTabChangesFocus(False)
        # Заголовки считаются от этого размера, поэтому он задан один раз здесь.
        self.setStyleSheet("QTextEdit { font-size: 14px; }")
        self.setPlaceholderText("Пишите здесь…")
        self.cursorPositionChanged.connect(self.state_changed)
        self.currentCharFormatChanged.connect(lambda _: self.state_changed.emit())

    # --- Markdown --------------------------------------------------------------

    def markdown(self) -> str:
        return self.toMarkdown().rstrip()

    def load_markdown(self, text: str) -> None:
        self.setMarkdown(text)
        self._split_line_breaks()
        self._decorate_all()
        self.document().setModified(False)
        self.document().clearUndoRedoStacks()

    def _split_line_breaks(self) -> None:
        """Перенос строки внутри абзаца делаем новым абзацем.

        Qt читает Markdown с «мягким» переносом внутри одного блока, а там нет ни
        флажка на второй строке, ни возможности выделить строку отдельно: любое
        оформление ляжет на весь блок. Каждая строка должна быть своим блоком.
        """
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        block = self.document().begin()
        while block.isValid():
            index = block.text().find(_LINE_BREAK)
            if index >= 0:
                at = QTextCursor(block)
                at.setPosition(block.position() + index)
                at.setPosition(block.position() + index + 1, QTextCursor.MoveMode.KeepAnchor)
                at.removeSelectedText()
                at.insertBlock()
            block = block.next()
        cursor.endEditBlock()

    def _decorate_all(self) -> None:
        block = self.document().begin()
        while block.isValid():
            self._decorate(block)
            block = block.next()

    @staticmethod
    def _decorate(block: QTextBlock) -> None:
        """Фон цитаты и кода. В Markdown его нет, поэтому ставится при каждом чтении."""
        kind = kind_of(block)
        fmt = QTextBlockFormat()
        if kind == CODE:
            fmt.setBackground(QColor(_CODE_BACKGROUND))
            fmt.setTopMargin(0)
            fmt.setBottomMargin(0)
        elif kind == QUOTE:
            fmt.setBackground(QColor(_QUOTE_BACKGROUND))
            fmt.setTopMargin(2)
            fmt.setBottomMargin(2)
        else:
            return
        QTextCursor(block).mergeBlockFormat(fmt)

    # --- состояние для панели --------------------------------------------------

    def current_kind(self) -> str:
        return kind_of(self.textCursor().block())

    def is_bold(self) -> bool:
        return self.fontWeight() >= QFont.Weight.Bold.value

    def is_italic(self) -> bool:
        return self.fontItalic()

    def is_inline_code(self) -> bool:
        """Курсор стоит в коде внутри строки (или выделен такой код)."""
        cursor = self.textCursor()
        if cursor.hasSelection():
            probe = QTextCursor(self.document())
            probe.setPosition(cursor.selectionStart() + 1)
            return probe.charFormat().fontFixedPitch()
        return self.currentCharFormat().fontFixedPitch()

    # --- знаки в строке --------------------------------------------------------

    def toggle_bold(self) -> None:
        weight = QFont.Weight.Normal if self.is_bold() else QFont.Weight.Bold
        fmt = QTextCharFormat()
        fmt.setFontWeight(weight.value)
        self.mergeCurrentCharFormat(fmt)

    def toggle_italic(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontItalic(not self.is_italic())
        self.mergeCurrentCharFormat(fmt)

    def insert_link(self) -> None:
        cursor = self.textCursor()
        selected = cursor.selectedText()
        url, accepted = QInputDialog.getText(
            self, "Ссылка", "Адрес (https://…):", text=_anchor_at(cursor))
        url = url.strip()
        if not accepted or not url:
            return
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(url)
        fmt.setForeground(Qt.GlobalColor.blue)
        fmt.setFontUnderline(True)
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        else:
            cursor.insertText(selected or url, fmt)
        self.setTextCursor(cursor)

    # --- блоки -----------------------------------------------------------------

    def toggle_inline_code(self) -> None:
        """Код только на выделенном куске строки, а не на всём абзаце."""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        cursor.beginEditBlock()
        try:
            if not self.is_inline_code():
                cursor.mergeCharFormat(_inline_code_format())
            else:
                spans = []
                block = self.document().findBlock(start)
                fragments = block.begin()
                while not fragments.atEnd():
                    fragment = fragments.fragment()
                    first = max(start, fragment.position())
                    last = min(end, fragment.position() + fragment.length())
                    if first < last:
                        spans.append((first, last, fragment.charFormat()))
                    fragments += 1
                for first, last, fmt in spans:
                    for prop in _CODE_TEXT_PROPERTIES:
                        fmt.clearProperty(prop)
                    span = QTextCursor(self.document())
                    span.setPosition(first)
                    span.setPosition(last, QTextCursor.MoveMode.KeepAnchor)
                    span.setCharFormat(fmt)
        finally:
            cursor.endEditBlock()
        self.state_changed.emit()

    def _selection_inside_one_line(self) -> bool:
        """Выделен кусок одной строки, а не вся строка и не несколько строк."""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return False
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        block = self.document().findBlock(start)
        if (block.position() != self.document().findBlock(end - 1).position()
                or kind_of(block) == CODE):
            return False
        return not (start <= block.position() and end >= block.position() + block.length() - 1)

    def toggle_kind(self, kind: str) -> None:
        """Применяет вид к выделенным блокам; если уже применён ко всем — снимает.

        Код на части строки — это код внутри строки, как в Notion: остальной текст
        строки остаётся обычным.
        """
        if kind == CODE and self._selection_inside_one_line():
            self.toggle_inline_code()
            return
        blocks = self._selected_blocks()
        target = PLAIN if all(kind_of(b) == kind for b in blocks) else kind
        cursor = self.textCursor()
        cursor.beginEditBlock()
        try:
            for block in blocks:
                self._to_plain(block)
            if target in (BULLET, NUMBER, CHECK):
                self._make_list(blocks, target)
            elif target != PLAIN:
                for block in blocks:
                    self._style_block(block, target)
        finally:
            cursor.endEditBlock()
        self.state_changed.emit()

    def insert_rule(self) -> None:
        """Горизонтальная черта после текущего блока и пустой абзац под ней."""
        cursor = self.textCursor()
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        cursor.insertBlock(_rule_format(), QTextCharFormat())
        cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
        cursor.endEditBlock()
        self.setTextCursor(cursor)

    def _selected_blocks(self) -> list[QTextBlock]:
        """Строки, которых коснулось выделение.

        Выделение «целой строки» (тройной щелчок, Shift+вниз) заканчивается в
        начале следующей. Эту строку выделенной не считаем — иначе оформление
        ляжет и на неё.
        """
        cursor = self.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        if end > start:
            end -= 1
        blocks = []
        block = self.document().findBlock(start)
        while block.isValid() and block.position() <= end:
            blocks.append(block)
            block = block.next()
        return blocks

    def _to_plain(self, block: QTextBlock) -> None:
        """Обычный абзац: без списка, заголовка, цитаты и кода, но с выделением внутри строки."""
        was_heading = block.blockFormat().headingLevel() > 0
        items = block.textList()
        if items is not None:
            items.remove(block)
        cursor = QTextCursor(block)
        cursor.setBlockFormat(QTextBlockFormat())
        # Размер и шрифт заголовка или кода сидят в тексте, а не в блоке. Убираем
        # только их: ссылки и курсив внутри строки остаются.
        fragments = block.begin()
        while not fragments.atEnd():
            fragment = fragments.fragment()
            fmt = fragment.charFormat()
            for prop in (QTextFormat.Property.FontSizeAdjustment, QTextFormat.Property.FontPointSize,
                         QTextFormat.Property.FontFamilies, QTextFormat.Property.FontFamily,
                         QTextFormat.Property.FontFixedPitch):
                fmt.clearProperty(prop)
            if was_heading:
                fmt.setFontWeight(QFont.Weight.Normal.value)
            span = QTextCursor(self.document())
            span.setPosition(fragment.position())
            span.setPosition(fragment.position() + fragment.length(), QTextCursor.MoveMode.KeepAnchor)
            span.setCharFormat(fmt)
            fragments += 1

    def _style_block(self, block: QTextBlock, kind: str) -> None:
        block_format, text_format = _sample(kind)
        cursor = QTextCursor(block)
        cursor.setBlockFormat(block_format)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cursor.mergeCharFormat(text_format)
        # Пустая строка: формат нужен тексту, который будет набран.
        cursor.mergeBlockCharFormat(text_format)
        self._decorate(block)

    def _make_list(self, blocks: list[QTextBlock], kind: str) -> None:
        style = (QTextListFormat.Style.ListDecimal if kind == NUMBER
                 else QTextListFormat.Style.ListDisc)
        previous = blocks[0].previous()
        items = previous.textList() if previous.isValid() else None
        # Продолжаем список выше, если он того же вида: иначе рядом окажутся два списка.
        if items is not None and (items.format().style() != style
                                  or (kind_of(previous) == CHECK) != (kind == CHECK)):
            items = None
        for block in blocks:
            if items is None:
                list_format = QTextListFormat()
                list_format.setStyle(style)
                items = QTextCursor(block).createList(list_format)
            else:
                items.add(block)
            if kind == CHECK:
                fmt = QTextBlockFormat()
                fmt.setMarker(QTextBlockFormat.MarkerType.Unchecked)
                QTextCursor(block).mergeBlockFormat(fmt)

    # --- клавиши и мышь --------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        modifiers = event.modifiers()
        if modifiers == Qt.KeyboardModifier.ControlModifier:
            if event.key() == Qt.Key.Key_B:
                self.toggle_bold()
                return
            if event.key() == Qt.Key.Key_I:
                self.toggle_italic()
                return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Shift+Enter в обычном Qt — перенос внутри абзаца: у пункта списка он
            # остаётся без флажка, а выделить такую строку отдельно нельзя. Здесь
            # любой Enter начинает новую строку-блок.
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                  modifiers & ~Qt.KeyboardModifier.ShiftModifier, "\r")
            if self._handle_enter():
                return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source: QMimeData) -> None:
        """Вставка: каждая строка буфера — своя строка-блок (в списке — свой пункт)."""
        if source.hasText():
            text = source.text().replace("\r\n", "\n").replace("\r", "\n").replace(_LINE_BREAK, "\n")
            self.textCursor().insertText(text)
            self.ensureCursorVisible()
            return
        super().insertFromMimeData(source)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        # Полоса слева у цитаты: фон один не отличает её от выделения.
        painter = QPainter(self.viewport())
        layout = self.document().documentLayout()
        shift_x = self.horizontalScrollBar().value()
        shift_y = self.verticalScrollBar().value()
        height = self.viewport().height()
        block = self.document().begin()
        while block.isValid():
            area = layout.blockBoundingRect(block).translated(-shift_x, -shift_y)
            if area.top() > height:
                break
            if area.bottom() >= 0 and kind_of(block) == QUOTE:
                painter.fillRect(
                    QRectF(area.left() + block.blockFormat().leftMargin() - 12, area.top(),
                           _QUOTE_BAR_WIDTH, area.height()),
                    QColor(_QUOTE_BAR))
            block = block.next()
        painter.end()

    def _handle_enter(self) -> bool:
        """Enter как в привычных редакторах: пустой пункт списка заканчивает список,
        а после заголовка начинается обычный абзац. True — клавиша обработана."""
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        block = cursor.block()
        kind = kind_of(block)
        if kind in (BULLET, NUMBER, CHECK) and not block.text():
            cursor.beginEditBlock()
            self._to_plain(block)
            cursor.endEditBlock()
            self.state_changed.emit()
            return True
        if kind in (H1, H2, H3) and cursor.atBlockEnd():
            cursor.beginEditBlock()
            cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
            cursor.endEditBlock()
            self.setTextCursor(cursor)
            return True
        return False

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        # Обычный щелчок по ссылке ставит курсор, чтобы её можно было править;
        # открывается она с Ctrl, как в большинстве редакторов.
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            href = self.anchorAt(event.position().toPoint())
            if href:
                QDesktopServices.openUrl(QUrl(href))
                return
        super().mouseReleaseEvent(event)


def _anchor_at(cursor: QTextCursor) -> str:
    return cursor.charFormat().anchorHref() if cursor.charFormat().isAnchor() else ""


class NoteToolbar(QWidget):
    """Кнопки оформления; подсвечивает то, что действует в месте курсора."""

    # (код, значок, подсказка, действие)
    def __init__(self, editor: NoteEditor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.editor = editor
        self._kind_buttons: dict[str, QToolButton] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        for kind, icon_name, tip in (
                (H1, "mdi6.format-header-1", "Крупный заголовок"),
                (H2, "mdi6.format-header-2", "Заголовок"),
                (H3, "mdi6.format-header-3", "Малый заголовок")):
            self._kind_buttons[kind] = self._button(layout, icon_name, tip, self._kind_action(kind))
        layout.addSpacing(8)
        self.bold = self._button(layout, "mdi6.format-bold", "Жирный (Ctrl+B)", editor.toggle_bold)
        self.italic = self._button(layout, "mdi6.format-italic", "Курсив (Ctrl+I)", editor.toggle_italic)
        self._button(layout, "mdi6.link-variant", "Ссылка (открывается с Ctrl)", editor.insert_link,
                     checkable=False)
        layout.addSpacing(8)
        for kind, icon_name, tip in (
                (CHECK, "mdi6.format-list-checks", "Список задач с флажками"),
                (BULLET, "mdi6.format-list-bulleted", "Маркированный список"),
                (NUMBER, "mdi6.format-list-numbered", "Нумерованный список"),
                (QUOTE, "mdi6.format-quote-open", "Цитата"),
                (CODE, "mdi6.code-tags", "Блок кода")):
            self._kind_buttons[kind] = self._button(layout, icon_name, tip, self._kind_action(kind))
        self._button(layout, "mdi6.minus", "Разделитель", editor.insert_rule, checkable=False)
        self._kind_buttons[CODE].setToolTip(
            "Код: выделенный кусок строки или вся строка, если ничего не выделено")
        layout.addStretch(1)

        editor.state_changed.connect(self.refresh)
        self.refresh()

    def _kind_action(self, kind: str) -> Callable[[], None]:
        return lambda: self.editor.toggle_kind(kind)

    def _button(self, layout: QHBoxLayout, icon_name: str, tip: str,
                action: Callable[[], None], checkable: bool = True) -> QToolButton:
        button = QToolButton(self)
        button.setIcon(icons.icon(icon_name, Palette.TEXT_MUTED))
        button.setToolTip(tip)
        button.setCheckable(checkable)
        button.setAutoRaise(True)
        # Фокус остаётся в тексте: иначе после щелчка по кнопке курсор пропадает.
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(lambda _checked=False: action())
        layout.addWidget(button)
        return button

    def refresh(self) -> None:
        kind = self.editor.current_kind()
        for code, button in self._kind_buttons.items():
            button.setChecked(code == kind or (code == CODE and self.editor.is_inline_code()))
        self.bold.setChecked(self.editor.is_bold())
        self.italic.setChecked(self.editor.is_italic())
