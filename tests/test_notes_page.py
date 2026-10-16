"""Вкладка «Заметки»: сохранение, дерево, поиск, файлы."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from app.core.notes import markdown_io as mio  # noqa: E402
from app.core.notes import store  # noqa: E402
from app.core.settings import AppSettings  # noqa: E402
from app.ui.notes_page import NotesPage  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class TestMarkdownFiles:
    def test_file_gets_title_as_first_heading(self) -> None:
        assert mio.to_file_text("План", "- [ ] дело") == "# План\n\n- [ ] дело\n"
        assert mio.to_file_text("План", "") == "# План\n"
        assert mio.to_file_text("", "текст") == "текст\n"

    def test_title_is_taken_back_from_the_heading(self) -> None:
        assert mio.from_file_text("# План\n\n- дело\n", "файл") == ("План", "- дело")
        assert mio.from_file_text("﻿\n# План ##\r\nтекст\r\n", "файл") == ("План", "текст")

    def test_without_heading_the_file_name_is_the_title(self) -> None:
        assert mio.from_file_text("просто текст", "черновик") == ("черновик", "просто текст")
        # Подзаголовок второго уровня — не заголовок страницы.
        assert mio.from_file_text("## раздел\n\nтекст", "файл")[0] == "файл"

    def test_export_then_import_is_lossless(self) -> None:
        body = "# Раздел\n\n- [x] готово\n\nтекст"
        assert mio.from_file_text(mio.to_file_text("Страница", body), "x") == ("Страница", body)

    def test_file_names_drop_forbidden_characters(self) -> None:
        assert mio.file_name_for('Отчёт: 1/2 "итог"?') == "Отчёт 1 2 итог"
        assert mio.file_name_for("  ... ") == "Без названия"
        assert len(mio.file_name_for("а" * 300)) == 80


class Notices:
    def __init__(self) -> None:
        self.items: list[tuple[str, object]] = []

    def __call__(self, text: str, kind: object) -> None:
        self.items.append((text, kind))


def make_page(app) -> tuple[NotesPage, Notices]:
    notices = Notices()
    page = NotesPage(AppSettings(), notices)
    page.resize(1100, 700)
    page.show()
    return page, notices


def write(page: NotesPage, title: str, body: str = "") -> None:
    """Правка так, как её вносит человек: с пометкой «изменено»."""
    page.title_edit.setText(title)
    page.title_edit.textEdited.emit(title)
    if body:
        page.editor.insertPlainText(body)


class TestPage:
    def test_empty_database_shows_invitation(self, app) -> None:
        page, _ = make_page(app)
        assert page.stack.currentIndex() == 0
        assert page.tree.topLevelItemCount() == 0

    def test_new_page_is_created_selected_and_saved(self, app) -> None:
        page, _ = make_page(app)
        page.new_page()
        assert page.stack.currentIndex() == 1
        write(page, "Дела", "купить хлеб")
        page.flush()
        assert [(n.title, n.body) for n in store.list_notes()] == [("Дела", "купить хлеб")]
        assert page.tree.topLevelItem(0).text(0) == "Дела"

    def test_untitled_page_gets_a_placeholder_name_in_tree(self, app) -> None:
        page, _ = make_page(app)
        page.new_page()
        assert page.tree.topLevelItem(0).text(0) == "Без названия"

    def test_autosave_fires_by_timer(self, app) -> None:
        page, _ = make_page(app)
        page.new_page()
        write(page, "Черновик")
        assert page._timer.isActive()
        page._timer.timeout.emit()
        assert store.list_notes()[0].title == "Черновик"

    def test_switching_pages_saves_the_one_being_left(self, app) -> None:
        page, _ = make_page(app)
        page.new_page()
        write(page, "Первая", "текст первой")
        page.new_page()
        notes = store.list_notes()
        assert [n.title for n in notes] == ["Первая", ""]
        assert notes[0].body == "текст первой"
        page.tree.setCurrentItem(page.tree.topLevelItem(0))
        assert page.title_edit.text() == "Первая"
        assert page.editor.markdown() == "текст первой"

    def test_leaving_the_tab_saves_pending_edit(self, app) -> None:
        page, _ = make_page(app)
        page.new_page()
        write(page, "Не потеряется")
        page.hide()
        assert store.list_notes()[0].title == "Не потеряется"

    def test_subpage_goes_under_its_parent(self, app) -> None:
        page, _ = make_page(app)
        page.new_page()
        parent_id = page._current
        page.new_page(parent_id)
        child = store.get_note(page._current)
        assert child is not None and child.parent_id == parent_id
        assert page.tree.topLevelItem(0).childCount() == 1

    def test_delete_asks_and_takes_nested_pages(self, app, monkeypatch) -> None:
        page, _ = make_page(app)
        page.new_page()
        root_id = page._current
        page.new_page(root_id)
        asked: list[str] = []

        def answer(parent, title, text, *args):
            asked.append(text)
            return QMessageBox.StandardButton.Yes

        monkeypatch.setattr(QMessageBox, "question", answer)
        page.delete_page(root_id)
        assert "вложенные страницы: 1" in asked[0]
        assert store.list_notes() == []
        assert page.stack.currentIndex() == 0

    def test_delete_declined_keeps_everything(self, app, monkeypatch) -> None:
        page, _ = make_page(app)
        page.new_page()
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
        page.delete_page()
        assert len(store.list_notes()) == 1

    def test_search_hides_other_pages_and_keeps_parents_of_matches(self, app) -> None:
        page, _ = make_page(app)
        page.new_page()
        write(page, "Работа")
        root_id = page._current
        page.new_page(root_id)
        write(page, "Звонки", "позвонить поставщику")
        page.new_page()
        write(page, "Дом", "купить хлеб")
        page.flush()
        page.search.setText("поставщику")
        hidden = {item.text(0): item.isHidden() for item in page._items.values()}
        assert hidden == {"Работа": False, "Звонки": False, "Дом": True}
        page.search.clear()
        assert not any(item.isHidden() for item in page._items.values())

    def test_drag_move_is_stored(self, app) -> None:
        page, _ = make_page(app)
        page.new_page()
        first = page._current
        page.new_page()
        second = page._current
        page._on_moved(second, first, 0)
        assert store.get_note(second).parent_id == first
        # Страница сама себе родитель — отказ, дерево перечитывается.
        page._on_moved(first, second, 0)
        assert store.get_note(first).parent_id is None

    def test_export_and_import_through_files(self, app, monkeypatch, tmp_path) -> None:
        page, _ = make_page(app)
        page.new_page()
        write(page, "План", "- [ ] дело")
        target = tmp_path / "plan.md"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
        page.export_page()
        assert target.read_text(encoding="utf-8").startswith("# План\n")

        monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(target)], ""))
        page.import_pages()
        assert [n.title for n in store.list_notes()] == ["План", "План"]
        assert page.title_edit.text() == "План"

    def test_import_reads_windows_cp1251_files(self, app, monkeypatch, tmp_path) -> None:
        page, _ = make_page(app)
        source = tmp_path / "заметка.txt"
        source.write_bytes("Привет, мир".encode("cp1251"))
        monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(source)], ""))
        page.import_pages()
        note = store.list_notes()[0]
        assert (note.title, note.body) == ("заметка", "Привет, мир")

    def test_save_error_is_reported_and_edit_is_kept(self, app, monkeypatch) -> None:
        page, notices = make_page(app)
        page.new_page()
        write(page, "Важное")

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("диск полон")

        monkeypatch.setattr(store, "save_note", broken)
        assert page._save_now() is False
        assert page._dirty
        assert any("не удалось сохранить" in text for text, _ in notices.items)


class TestInWindow:
    """Вкладка в главном окне: порядок, горячие клавиши, закрытие."""

    @pytest.fixture
    def window(self, app):
        from app.core.payments import transport
        from app.ui.main_window import MainWindow

        transport.session.clear()
        window = MainWindow(AppSettings(), offline=True)
        yield window
        transport.session.clear()

    def test_tab_sits_where_pages_list_says(self, window) -> None:
        from app.ui.pages import PAGES, index_of
        from app.ui.main_window import _page_of

        index = index_of("notes")
        assert PAGES[index].title == "Заметки"
        assert _page_of(window.pages.widget(index)) is window.notes_page
        assert not window._nav_group.button(index).isHidden()

    def test_ctrl_s_saves_the_note_and_not_matching_results(self, window, monkeypatch) -> None:
        from app.ui.pages import index_of

        window.show_page(index_of("notes"))
        page = window.notes_page
        page.new_page()
        write(page, "Через Ctrl+S")
        monkeypatch.setattr(window.match_page, "save_results",
                            lambda: pytest.fail("Ctrl+S ушёл в сопоставление"))
        window._save_current()
        assert store.list_notes()[0].title == "Через Ctrl+S"

    def test_f5_stays_on_the_tab(self, window) -> None:
        from app.ui.pages import index_of

        window.show_page(index_of("notes"))
        window._run_current()
        assert window.pages.currentIndex() == index_of("notes")

    def test_closing_the_window_saves_pending_edit(self, window) -> None:
        from app.ui.pages import index_of

        window.show_page(index_of("notes"))
        window.notes_page.new_page()
        write(window.notes_page, "Не потерять при выходе")
        window.close()
        assert store.list_notes()[0].title == "Не потерять при выходе"
