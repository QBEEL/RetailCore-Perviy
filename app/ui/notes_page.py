"""Вкладка «Заметки»: дерево страниц слева, текст страницы справа.

Заметки личные и лежат на этом компьютере. Сохраняются сами: через
полсекунды после последней правки, при переходе на другую страницу и при
уходе с вкладки, — кнопки «Сохранить» нет, потому что забытая кнопка и есть
потерянная запись.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Callable

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.notes import markdown_io, store
from ..core.settings import AppSettings
from . import icons
from .theme import Metrics, Palette
from .widgets.common import Card, Hint, SectionTitle, Subtitle, Title, fade_in
from .widgets.note_editor import NoteEditor, NoteToolbar
from .widgets.toast import ToastKind

_ID = Qt.ItemDataRole.UserRole
SAVE_DELAY_MS = 800
UNTITLED = "Без названия"

_TITLE_STYLE = (
    "QLineEdit { border: none; background: transparent; padding: 2px 0;"
    " font-size: 24px; font-weight: 700; }"
    " QLineEdit:focus { border: none; }")


class PageTree(QTreeWidget):
    """Дерево страниц; перетаскивание вкладывает страницу или переставляет её среди соседей."""

    # номер страницы, новый родитель (None — верхний уровень), место среди соседей
    moved = Signal(int, object, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setIndentation(16)
        self.setAnimated(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def dropEvent(self, event) -> None:
        item = self.currentItem()
        super().dropEvent(event)
        if item is None:
            return
        parent = item.parent()
        index = parent.indexOfChild(item) if parent is not None else self.indexOfTopLevelItem(item)
        if index < 0:
            return
        self.moved.emit(item.data(0, _ID), parent.data(0, _ID) if parent is not None else None, index)


class NotesPage(QWidget):
    """Личные страницы с форматированным текстом и списками дел."""

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, ToastKind], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._current: int | None = None
        self._items: dict[int, QTreeWidgetItem] = {}
        # Идёт загрузка страницы в поля или перестройка дерева: правки в это время
        # не от человека, и сохранять их незачем.
        self._loading = False
        self._reloading = False
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY_MS)
        self._timer.timeout.connect(self._save_now)
        self._build()
        self._reload_tree()

    # --- интерфейс ------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(Title("Заметки", self))
        root.addWidget(Subtitle(
            "Личные страницы, списки дел и черновики. Хранятся на этом компьютере и "
            "сохраняются сами.", self))

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._tree_card())
        splitter.addWidget(self._editor_card())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([290, 880])
        # В невысоком окне страница прокручивается, а не сжимает редактор в щель.
        splitter.setMinimumHeight(460)
        root.addWidget(splitter, 1)

    def _tree_card(self) -> Card:
        card = Card(self, padding=Metrics.PAD - 4)
        body = card.body()
        body.setSpacing(Metrics.GAP - 4)

        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(SectionTitle("Страницы", card), 1)
        self.new_button = QPushButton("Новая", card)
        self.new_button.setIcon(icons.icon("plus"))
        self.new_button.setToolTip("Новая страница")
        self.new_button.clicked.connect(lambda: self.new_page())
        header.addWidget(self.new_button)
        self.import_button = QPushButton("Импорт…", card)
        self.import_button.setToolTip("Добавить страницы из файлов .md или .txt")
        self.import_button.clicked.connect(self.import_pages)
        header.addWidget(self.import_button)
        body.addLayout(header)

        self.search = QLineEdit(card)
        self.search.setPlaceholderText("Поиск по страницам…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        body.addWidget(self.search)

        self.tree = PageTree(card)
        self.tree.setStyleSheet(
            f"QTreeWidget {{ border: 1px solid {Palette.BORDER}; border-radius: {Metrics.RADIUS_SM}px;"
            f" background: {Palette.SURFACE}; padding: 4px; outline: none; }}"
            " QTreeWidget::item { padding: 4px 2px; }")
        self.tree.currentItemChanged.connect(self._on_current_changed)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.moved.connect(self._on_moved)
        for text, key, handler in (
                ("Удалить страницу", Qt.Key.Key_Delete, lambda: self.delete_page()),
                ("Переименовать", Qt.Key.Key_F2, lambda: self.rename_page())):
            action = QAction(text, self.tree)
            action.setShortcut(QKeySequence(key))
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.triggered.connect(handler)
            self.tree.addAction(action)
        body.addWidget(self.tree, 1)
        body.addWidget(Hint(
            "Перетащите страницу на другую, чтобы вложить её, или между страницами, "
            "чтобы переставить. Правая кнопка — меню.", card))
        return card

    def _editor_card(self) -> Card:
        card = Card(self, padding=Metrics.PAD)
        self.stack = QStackedWidget(card)
        self.stack.addWidget(self._empty_state())
        self.stack.addWidget(self._editing_state())
        card.body().addWidget(self.stack, 1)
        return card

    def _empty_state(self) -> QWidget:
        box = QWidget(self)
        layout = QVBoxLayout(box)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(Metrics.GAP)
        headline = QLabel("Здесь пока пусто", box)
        headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        headline.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(headline)
        hint = Hint("Создайте первую страницу: для заметки, списка дел или черновика.", box)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)
        create = QPushButton("Создать страницу", box)
        create.setObjectName("Primary")
        create.clicked.connect(lambda: self.new_page())
        layout.addWidget(create, 0, Qt.AlignmentFlag.AlignCenter)
        return box

    def _editing_state(self) -> QWidget:
        box = QWidget(self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Metrics.GAP - 4)

        self.title_edit = QLineEdit(box)
        self.title_edit.setPlaceholderText(UNTITLED)
        self.title_edit.setStyleSheet(_TITLE_STYLE)
        self.title_edit.setMaxLength(200)
        self.title_edit.textEdited.connect(self._on_title_edited)
        layout.addWidget(self.title_edit)

        self.editor = NoteEditor(box)
        self.editor.setFrameShape(NoteEditor.Shape.NoFrame)
        self.editor.document().contentsChanged.connect(self._on_text_changed)
        layout.addWidget(NoteToolbar(self.editor, box))
        layout.addWidget(self.editor, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.status = QLabel("", box)
        self.status.setStyleSheet(f"color: {Palette.TEXT_FAINT};")
        footer.addWidget(self.status, 1)
        self.export_button = QPushButton("Экспорт…", box)
        self.export_button.setToolTip("Сохранить страницу в файл .md")
        self.export_button.clicked.connect(self.export_page)
        footer.addWidget(self.export_button)
        self.delete_button = QPushButton("Удалить", box)
        self.delete_button.setObjectName("Danger")
        self.delete_button.setIcon(icons.icon("trash", Palette.DANGER))
        self.delete_button.clicked.connect(lambda: self.delete_page())
        footer.addWidget(self.delete_button)
        layout.addLayout(footer)
        return box

    # --- дерево ---------------------------------------------------------------

    def restore(self) -> None:
        """Вкладку открыли: перечитываем дерево, оставаясь на той же странице."""
        self._reload_tree(select=self._current)

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _reload_tree(self, select: int | None = None) -> None:
        expanded = {note_id for note_id, item in self._items.items() if item.isExpanded()}
        self._reloading = True
        try:
            self.tree.clear()
            self._items = {}
            try:
                notes = store.list_notes()
            except sqlite3.Error as error:
                notes = []
                self.notify(f"Заметки не читаются: {error}", ToastKind.ERROR)
            children: dict[int | None, list] = {}
            for note in notes:
                children.setdefault(note.parent_id, []).append(note)

            def add(parent: QTreeWidgetItem | None, parent_id: int | None) -> None:
                for note in children.get(parent_id, []):
                    item = QTreeWidgetItem([note.title or UNTITLED])
                    item.setData(0, _ID, note.id)
                    item.setIcon(0, icons.icon("mdi6.file-document-outline"))
                    if parent is None:
                        self.tree.addTopLevelItem(item)
                    else:
                        parent.addChild(item)
                    self._items[note.id] = item
                    add(item, note.id)

            add(None, None)
            for note_id in expanded & self._items.keys():
                self._items[note_id].setExpanded(True)
        finally:
            self._reloading = False
        self._apply_filter()

        target = select if select in self._items else self._current
        if target not in self._items:
            first = self.tree.topLevelItem(0)
            target = first.data(0, _ID) if first is not None else None
        if target is None:
            self._open(None)
        else:
            self.tree.setCurrentItem(self._items[target])
            if target != self._current:
                self._open(target)

    def _apply_filter(self) -> None:
        text = self.search.text().strip()
        if not text:
            for item in self._items.values():
                item.setHidden(False)
            return
        # Поиск идёт по сохранённому, поэтому недописанную правку сначала сохраняем.
        self._save_now()
        try:
            matches = store.search(text)
        except sqlite3.Error:
            matches = set()
        visible: set[int] = set()
        for note_id in matches:
            item = self._items.get(note_id)
            while item is not None:
                visible.add(item.data(0, _ID))
                item = item.parent()
                if item is not None:
                    item.setExpanded(True)
        for note_id, item in self._items.items():
            item.setHidden(note_id not in visible)

    def _on_current_changed(self, current: QTreeWidgetItem | None, _previous) -> None:
        if self._reloading:
            return
        note_id = current.data(0, _ID) if current is not None else None
        if note_id == self._current:
            return
        # _current ещё указывает на прежнюю страницу — её и сохраняем.
        self._save_now()
        self._open(note_id)

    def _tree_menu(self, point) -> None:
        item = self.tree.itemAt(point)
        menu = QMenu(self.tree)
        if item is None:
            menu.addAction("Новая страница", lambda: self.new_page())
        else:
            note_id = item.data(0, _ID)
            menu.addAction("Вложенная страница", lambda: self.new_page(note_id))
            menu.addAction("Переименовать", lambda: self.rename_page(note_id))
            menu.addSeparator()
            menu.addAction("Удалить…", lambda: self.delete_page(note_id))
        menu.exec(self.tree.viewport().mapToGlobal(point))

    def _on_moved(self, note_id: int, parent_id: int | None, index: int) -> None:
        try:
            store.move_note(note_id, parent_id, index)
        except (ValueError, sqlite3.Error) as error:
            self.notify(f"Страницу не удалось перенести: {error}", ToastKind.ERROR)
            self._reload_tree(select=self._current)
            return
        if parent_id is not None and parent_id in self._items:
            self._items[parent_id].setExpanded(True)

    # --- страница -------------------------------------------------------------

    def _open(self, note_id: int | None) -> None:
        self._current = note_id
        self._dirty = False
        self._timer.stop()
        if note_id is None:
            self.stack.setCurrentIndex(0)
            return
        try:
            note = store.get_note(note_id)
        except sqlite3.Error as error:
            self.notify(f"Страница не открывается: {error}", ToastKind.ERROR)
            note = None
        if note is None:
            # Страницы уже нет: убираем её из дерева, а не показываем пустое место.
            self._current = None
            self._reload_tree()
            return
        self._loading = True
        try:
            self.title_edit.setText(note.title)
            self.editor.load_markdown(note.body)
        finally:
            self._loading = False
        self.status.setText(f"Изменено {_stamp(note.updated_at)}")
        if self.stack.currentIndex() != 1:
            self.stack.setCurrentIndex(1)
            fade_in(self.stack.currentWidget(), 160)

    def new_page(self, parent_id: int | None = None) -> None:
        if not self._save_now():
            return
        if self.search.text():
            # Новая страница не совпадёт с запросом и осталась бы скрытой.
            self.search.clear()
        try:
            note = store.create_note(parent_id)
        except (sqlite3.Error, ValueError) as error:
            self.notify(f"Страницу не удалось создать: {error}", ToastKind.ERROR)
            return
        self._reload_tree(select=note.id)
        if parent_id is not None and parent_id in self._items:
            self._items[parent_id].setExpanded(True)
        self.title_edit.setFocus()

    def rename_page(self, note_id: int | None = None) -> None:
        if note_id is not None and note_id in self._items:
            self.tree.setCurrentItem(self._items[note_id])
        if self._current is None:
            return
        self.title_edit.setFocus()
        self.title_edit.selectAll()

    def delete_page(self, note_id: int | None = None) -> None:
        note_id = note_id if note_id is not None else self._current
        if note_id is None or note_id not in self._items:
            return
        title = self._items[note_id].text(0)
        try:
            nested = len(store.descendants(note_id))
        except sqlite3.Error:
            nested = 0
        text = f"Удалить страницу «{title}»?"
        if nested:
            text += f"\nВместе с ней удалятся вложенные страницы: {nested}."
        text += "\nЭто нельзя отменить."
        answer = QMessageBox.question(
            self, "Удалить страницу", text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        # Удаляемую страницу не сохраняем: правка, ждущая таймера, ей уже не нужна.
        if self._current == note_id:
            self._dirty = False
            self._timer.stop()
        try:
            store.delete_note(note_id)
        except sqlite3.Error as error:
            self.notify(f"Страницу не удалось удалить: {error}", ToastKind.ERROR)
            return
        self._save_now()
        self._reload_tree()

    # --- сохранение -----------------------------------------------------------

    def _on_title_edited(self, text: str) -> None:
        if self._loading or self._current is None:
            return
        item = self._items.get(self._current)
        if item is not None:
            item.setText(0, text.strip() or UNTITLED)
        self._mark_dirty()

    def _on_text_changed(self) -> None:
        if not self._loading and self._current is not None:
            self._mark_dirty()

    def _mark_dirty(self) -> None:
        self._dirty = True
        self.status.setText("Изменения не сохранены…")
        self._timer.start()

    def _save_now(self) -> bool:
        """Сохраняет открытую страницу, если в ней что-то менялось. False — не вышло."""
        self._timer.stop()
        if self._current is None or not self._dirty:
            return True
        try:
            store.save_note(self._current, self.title_edit.text().strip(), self.editor.markdown())
        except sqlite3.Error as error:
            self.notify(f"Заметку не удалось сохранить: {error}", ToastKind.ERROR)
            # Правка остаётся несохранённой — повторим при следующем случае.
            return False
        self._dirty = False
        self.status.setText(f"Сохранено {datetime.now():%H:%M}")
        return True

    def flush(self) -> None:
        """Сохраняет, что не успело сохраниться: при уходе с вкладки и закрытии окна."""
        self._save_now()

    def save_now(self) -> None:
        """Ctrl+S: сохранить сразу и сказать об этом."""
        if self._current is not None and self._save_now():
            self.notify("Страница сохранена.", ToastKind.SUCCESS)

    def hideEvent(self, event) -> None:
        self.flush()
        super().hideEvent(event)

    # --- файлы ----------------------------------------------------------------

    def export_page(self) -> None:
        if self._current is None or not self._save_now():
            return
        title = self.title_edit.text().strip()
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт страницы", markdown_io.file_name_for(title) + ".md",
            "Markdown (*.md);;Все файлы (*.*)")
        if not path:
            return
        text = markdown_io.to_file_text(title, self.editor.markdown())
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except OSError as error:
            self.notify(f"Файл не записан: {error}", ToastKind.ERROR)
            return
        self.notify(f"Страница сохранена в {os.path.basename(path)}.", ToastKind.SUCCESS)

    def import_pages(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Импорт страниц", "", "Текст и Markdown (*.md *.markdown *.txt);;Все файлы (*.*)")
        if not paths or not self._save_now():
            return
        if self.search.text():
            self.search.clear()
        created: list[int] = []
        for path in paths:
            try:
                title, body = markdown_io.from_file_text(
                    _read_text(path), markdown_io.title_from_path(path))
                created.append(store.create_note(None, title, body).id)
            except (OSError, sqlite3.Error) as error:
                self.notify(f"{os.path.basename(path)}: не удалось добавить ({error}).",
                            ToastKind.ERROR)
        if created:
            self._reload_tree(select=created[-1])
            self.notify(
                "Страница добавлена." if len(created) == 1 else f"Страниц добавлено: {len(created)}.",
                ToastKind.SUCCESS)


def _read_text(path: str) -> str:
    """Файл из блокнота Windows бывает в cp1251, а не в UTF-8."""
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1251")


def _stamp(value: str) -> str:
    try:
        return f"{datetime.fromisoformat(value):%d.%m.%Y %H:%M}"
    except ValueError:
        return value
