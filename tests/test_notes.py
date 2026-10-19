"""Заметки: дерево страниц, поиск, перенос и удаление."""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.notes import schema, store  # noqa: E402
from app.ui.widgets import note_editor as ne  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def titles(parent: int | None = None) -> list[str]:
    return [n.title for n in store.list_notes() if n.parent_id == parent]


class TestStore:
    def test_new_pages_go_to_the_end_among_siblings(self) -> None:
        store.create_note(title="Первая")
        store.create_note(title="Вторая")
        assert titles() == ["Первая", "Вторая"]

    def test_nested_pages_keep_their_own_order(self) -> None:
        root = store.create_note(title="Корень")
        store.create_note(root.id, title="а")
        store.create_note(root.id, title="б")
        assert titles(root.id) == ["а", "б"]
        assert titles() == ["Корень"]

    def test_create_under_missing_parent_is_refused(self) -> None:
        with pytest.raises(ValueError):
            store.create_note(999)

    def test_save_updates_text_and_reports_missing_page(self) -> None:
        note = store.create_note(title="Старое")
        assert store.save_note(note.id, "Новое", "- [ ] дело")
        saved = store.get_note(note.id)
        assert saved is not None and (saved.title, saved.body) == ("Новое", "- [ ] дело")
        assert not store.save_note(999, "x", "y")

    def test_move_to_another_parent_and_position(self) -> None:
        a = store.create_note(title="А")
        b = store.create_note(title="Б")
        c = store.create_note(title="В")
        store.move_note(c.id, None, 0)
        assert titles() == ["В", "А", "Б"]
        store.move_note(a.id, b.id)
        assert titles() == ["В", "Б"]
        assert titles(b.id) == ["А"]

    def test_move_into_itself_or_descendant_is_refused(self) -> None:
        root = store.create_note(title="Корень")
        child = store.create_note(root.id, title="Дочерняя")
        grandchild = store.create_note(child.id, title="Внучка")
        for target in (root.id, child.id, grandchild.id):
            with pytest.raises(ValueError):
                store.move_note(root.id, target)
        # Ничего не сломалось: дерево прежнее.
        assert titles() == ["Корень"]
        assert titles(grandchild.id) == []

    def test_delete_takes_nested_pages_and_counts_them(self) -> None:
        root = store.create_note(title="Корень")
        child = store.create_note(root.id, title="Дочерняя")
        store.create_note(child.id, title="Внучка")
        other = store.create_note(title="Другая")
        assert store.delete_note(root.id) == 3
        assert [n.id for n in store.list_notes()] == [other.id]
        assert store.delete_note(root.id) == 0

    def test_descendants(self) -> None:
        root = store.create_note(title="Корень")
        child = store.create_note(root.id)
        grandchild = store.create_note(child.id)
        assert sorted(store.descendants(root.id)) == sorted([child.id, grandchild.id])

    def test_search_ignores_case_and_yo_and_looks_into_text(self) -> None:
        first = store.create_note(title="Ёлки", body="купить ГИРЛЯНДУ")
        second = store.create_note(title="Дела", body="позвонить поставщику")
        assert store.search("елки") == {first.id}
        assert store.search("гирлянду ёлки") == {first.id}
        assert store.search("ПОСТАВЩИКУ") == {second.id}
        assert store.search("нет такого") == set()

    def test_empty_search_matches_everything(self) -> None:
        pages = {store.create_note(title=name).id for name in ("раз", "два")}
        assert store.search("  ") == pages

    def test_search_follows_edits(self) -> None:
        note = store.create_note(title="Черновик")
        store.save_note(note.id, "Черновик", "про молоко")
        assert store.search("молоко") == {note.id}
        store.save_note(note.id, "Черновик", "про хлеб")
        assert store.search("молоко") == set()


class TestSchema:
    def test_repeated_migration_changes_nothing(self, tmp_path) -> None:
        path = str(tmp_path / "notes.db")
        note = store.create_note(title="Живёт", path=path)
        with store.connect(path) as connection:
            assert schema.migrate(connection) == schema.VERSION
        assert store.get_note(note.id, path) is not None

    def test_database_from_newer_version_is_left_alone(self, tmp_path) -> None:
        path = str(tmp_path / "notes.db")
        store.create_note(title="Живёт", path=path)
        raw = sqlite3.connect(path)
        raw.execute(f"PRAGMA user_version = {schema.VERSION + 5}")
        raw.commit()
        raw.close()
        with store.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == schema.VERSION + 5


def _blocks(box: ne.NoteEditor) -> list:
    block, found = box.document().begin(), []
    while block.isValid():
        found.append(block)
        block = block.next()
    return found


def editor(app, markdown: str = "") -> ne.NoteEditor:
    box = ne.NoteEditor()
    box.resize(400, 300)
    box.show()
    box.load_markdown(markdown)
    return box


def runs(box: ne.NoteEditor) -> list[tuple[str, bool, bool]]:
    """Куски текста с признаками «жирный» и «курсив».

    Жирный и курсив проверяются по формату текста, а не по строке Markdown:
    без настоящих шрифтов (тестовая платформа offscreen) Qt не пишет ни `**`,
    ни `*`, хотя в приложении они пишутся.
    """
    found = []
    block = box.document().begin()
    while block.isValid():
        fragments = block.begin()
        while not fragments.atEnd():
            fragment = fragments.fragment()
            fmt = fragment.charFormat()
            found.append((fragment.text(), fmt.fontWeight() >= 700, fmt.fontItalic()))
            fragments += 1
        block = block.next()
    return found


def put_cursor(box: ne.NoteEditor, block: int, end: bool = True) -> None:
    cursor = QTextCursor(box.document().findBlockByNumber(block))
    if end:
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
    box.setTextCursor(cursor)


class TestEditor:
    def test_markdown_round_trip_keeps_structure(self, app) -> None:
        source = ("# Заголовок\n\nОбычный **жирный** и *курсив*.\n\n"
                  "- [ ] позвонить\n- [x] отправить\n\n1. раз\n2. два\n")
        box = editor(app, source)
        assert ("жирный", True, False) in runs(box)
        assert ("курсив", False, True) in runs(box)
        text = box.markdown()
        for line in ("# Заголовок", "- [ ] позвонить", "- [x] отправить"):
            assert line in text.splitlines()
        # Qt пишет номера списка со своими пробелами; главное, что после
        # перечитывания ничего не меняется и пункты на месте.
        again = editor(app, text)
        assert again.markdown() == text
        assert [b.text() for b in _blocks(again)][-2:] == ["раз", "два"]

    def test_heading_toggle_writes_hashes_and_back(self, app) -> None:
        box = editor(app, "просто текст")
        put_cursor(box, 0)
        box.toggle_kind(ne.H2)
        assert box.markdown() == "## просто текст"
        assert box.current_kind() == ne.H2
        box.toggle_kind(ne.H2)
        assert box.markdown() == "просто текст"
        assert box.current_kind() == ne.PLAIN

    def test_switching_heading_level(self, app) -> None:
        box = editor(app, "## тема")
        put_cursor(box, 0)
        box.toggle_kind(ne.H1)
        assert box.markdown() == "# тема"

    def test_checklist_and_its_click(self, app) -> None:
        box = editor(app, "дело")
        put_cursor(box, 0)
        box.toggle_kind(ne.CHECK)
        assert box.markdown() == "- [ ] дело"
        assert box.current_kind() == ne.CHECK
        # Флажок переключается щелчком — Qt делает это сам.
        block = box.document().findBlockByNumber(0)
        rect = box.cursorRect(QTextCursor(block))
        QTest.mouseClick(box.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(rect.x() - 12, rect.center().y()))
        assert box.markdown() == "- [x] дело"
        box.toggle_kind(ne.CHECK)
        assert box.markdown() == "дело"

    def test_bullets_and_numbers_over_several_lines(self, app) -> None:
        box = editor(app, "раз\n\nдва\n\nтри")
        cursor = box.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(box.document().characterCount() - 1, QTextCursor.MoveMode.KeepAnchor)
        box.setTextCursor(cursor)
        box.toggle_kind(ne.NUMBER)
        # Qt ставит после номера два пробела; для Markdown это то же самое.
        assert re.sub(r"(\d\.) +", r"\1 ", box.markdown()) == "1. раз\n2. два\n3. три"
        box.toggle_kind(ne.BULLET)
        assert box.markdown() == "- раз\n- два\n- три"

    def test_quote_and_code(self, app) -> None:
        box = editor(app, "цитата")
        put_cursor(box, 0)
        box.toggle_kind(ne.QUOTE)
        assert box.markdown() == "> цитата"
        box.toggle_kind(ne.CODE)
        assert box.markdown() == "```\nцитата\n```"
        box.toggle_kind(ne.CODE)
        assert box.markdown() == "цитата"

    def test_bold_and_italic_on_selection(self, app) -> None:
        box = editor(app, "слово")
        cursor = box.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        box.setTextCursor(cursor)
        box.toggle_bold()
        assert runs(box) == [("слово", True, False)]
        box.toggle_bold()
        box.toggle_italic()
        assert runs(box) == [("слово", False, True)]

    def test_rule_is_inserted_with_a_paragraph_below(self, app) -> None:
        box = editor(app, "до")
        put_cursor(box, 0)
        box.insert_rule()
        assert "- - -" in box.markdown()
        # keyClicks с кириллицей в тестовом Qt падает; ввод ОС ей не страдает.
        QTest.keyClicks(box, "after")
        text = box.markdown()
        assert text.startswith("до") and text.endswith("after")

    def test_enter_on_empty_list_item_ends_the_list(self, app) -> None:
        box = editor(app, "- дело")
        put_cursor(box, 0)
        QTest.keyClick(box, Qt.Key.Key_Return)
        assert box.current_kind() == ne.BULLET
        QTest.keyClick(box, Qt.Key.Key_Return)
        assert box.current_kind() == ne.PLAIN
        QTest.keyClicks(box, "plain")
        assert box.markdown() == "- дело\n\nplain"

    def test_enter_after_heading_starts_plain_paragraph(self, app) -> None:
        box = editor(app, "# Тема")
        put_cursor(box, 0)
        QTest.keyClick(box, Qt.Key.Key_Return)
        QTest.keyClicks(box, "text")
        assert box.markdown() == "# Тема\n\ntext"

    def test_ctrl_b_toggles_bold_without_window_actions(self, app) -> None:
        box = editor(app, "слово")
        cursor = box.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        box.setTextCursor(cursor)
        # Событие подаётся прямо в редактор. QTest.keyClick с Ctrl оставляет в Qt
        # «зажатый Ctrl», и у следующих тестов selectRow превращается в переключение.
        box.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier))
        assert runs(box) == [("слово", True, False)]

    def test_pasted_formatting_is_dropped(self, app) -> None:
        box = editor(app)
        assert not box.acceptRichText()

    def test_toolbar_follows_cursor(self, app) -> None:
        box = editor(app, "# Тема\n\n- [ ] дело")
        bar = ne.NoteToolbar(box)
        put_cursor(box, 0)
        assert bar._kind_buttons[ne.H1].isChecked()
        put_cursor(box, 1)
        assert bar._kind_buttons[ne.CHECK].isChecked()
        assert not bar._kind_buttons[ne.H1].isChecked()


def kinds(box: ne.NoteEditor) -> list[tuple[str, str]]:
    return [(ne.kind_of(b), b.text()) for b in _blocks(box)]


def select_text(box: ne.NoteEditor, block: int, start: int, end: int) -> None:
    """Выделение внутри строки: символы start..end блока с номером block."""
    first = box.document().findBlockByNumber(block)
    cursor = QTextCursor(box.document())
    cursor.setPosition(first.position() + start)
    cursor.setPosition(first.position() + end, QTextCursor.MoveMode.KeepAnchor)
    box.setTextCursor(cursor)


def shift_enter(box: ne.NoteEditor) -> None:
    # Событие подаётся прямо: QTest с модификатором оставляет его «зажатым» для остальных тестов.
    box.keyPressEvent(QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier, "\r"))


class TestLines:
    """Каждая строка — свой блок: флажок, код и выделение действуют на неё одну."""

    def test_shift_enter_in_checklist_starts_a_new_item_with_its_own_box(self, app) -> None:
        box = editor(app, "- [ ] первое")
        put_cursor(box, 0)
        shift_enter(box)
        box.insertPlainText("второе")
        assert kinds(box) == [(ne.CHECK, "первое"), (ne.CHECK, "второе")]
        assert box.markdown() == "- [ ] первое\n- [ ] второе"

    def test_line_break_inside_paragraph_is_split_on_load(self, app) -> None:
        # Жёсткий перенос Markdown Qt читает как перенос внутри одного блока.
        box = editor(app, "- [ ] первое\\\nвторое\n- [ ] третье")
        assert kinds(box) == [(ne.CHECK, "первое"), (ne.CHECK, "второе"), (ne.CHECK, "третье")]

    def test_pasted_lines_become_items_of_the_checklist(self, app) -> None:
        from PySide6.QtCore import QMimeData
        box = editor(app, "- [ ] пункт")
        put_cursor(box, 0)
        data = QMimeData()
        data.setText("\r\nдва\r\nтри")
        box.insertFromMimeData(data)
        assert kinds(box) == [(ne.CHECK, "пункт"), (ne.CHECK, "два"), (ne.CHECK, "три")]

    def test_selecting_a_whole_line_does_not_touch_the_next_one(self, app) -> None:
        box = editor(app, "один\n\nдва\n\nтри")
        # Так выделяет тройной щелчок: от начала строки до начала следующей.
        cursor = QTextCursor(box.document().findBlockByNumber(1))
        cursor.setPosition(box.document().findBlockByNumber(1).position())
        cursor.setPosition(box.document().findBlockByNumber(2).position(),
                           QTextCursor.MoveMode.KeepAnchor)
        box.setTextCursor(cursor)
        assert [b.text() for b in box._selected_blocks()] == ["два"]
        box.toggle_kind(ne.CODE)
        assert kinds(box) == [(ne.PLAIN, "один"), (ne.CODE, "два"), (ne.PLAIN, "три")]

    def test_checklist_on_one_selected_line_leaves_the_others(self, app) -> None:
        box = editor(app, "один\n\nдва\n\nтри")
        select_text(box, 1, 0, 3)
        box.toggle_kind(ne.CHECK)
        assert kinds(box) == [(ne.PLAIN, "один"), (ne.CHECK, "два"), (ne.PLAIN, "три")]

    def test_selected_piece_of_a_line_becomes_inline_code(self, app) -> None:
        box = editor(app, "слово другое слово")
        select_text(box, 0, 6, 12)
        box.toggle_kind(ne.CODE)
        # Блок остался обычным, кодом стал только выделенный кусок.
        assert kinds(box) == [(ne.PLAIN, "слово другое слово")]
        assert box.markdown().startswith("слово `другое` слово")
        select_text(box, 0, 6, 12)
        assert box.is_inline_code()
        box.toggle_kind(ne.CODE)
        assert "`" not in box.markdown()

    def test_code_without_selection_still_makes_the_whole_line_code(self, app) -> None:
        box = editor(app, "одна строка")
        put_cursor(box, 0)
        box.toggle_kind(ne.CODE)
        assert kinds(box) == [(ne.CODE, "одна строка")]

    def test_quote_and_code_blocks_are_tinted_even_after_reopening(self, app) -> None:
        box = editor(app, "> цитата\n\n```\nкод\n```\n\nобычный")
        backgrounds = [b.blockFormat().background().style() != Qt.BrushStyle.NoBrush
                       for b in _blocks(box)]
        assert backgrounds == [True, True, False]
        # Снятие вида убирает и фон.
        put_cursor(box, 0)
        box.toggle_kind(ne.QUOTE)
        assert _blocks(box)[0].blockFormat().background().style() == Qt.BrushStyle.NoBrush

    def test_quote_bar_is_painted(self, app) -> None:
        box = editor(app, "> цитата\n\nобычный")
        box.resize(400, 200)
        image = box.viewport().grab().toImage()
        rect = box.cursorRect(QTextCursor(box.document().findBlockByNumber(0)))
        bar_x = rect.x() - 12 + 1
        pixel = image.pixelColor(bar_x, rect.center().y())
        assert (pixel.red(), pixel.green(), pixel.blue()) != (255, 255, 255)
