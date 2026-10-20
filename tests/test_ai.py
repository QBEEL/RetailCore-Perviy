"""ИИ-правка заметок: запрос к серверу, окно и кнопка на странице."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.core import ai  # noqa: E402
from app.core.notes import store  # noqa: E402
from app.core.payments import transport  # noqa: E402
from app.core.payments.transport import AuthError, ServerError  # noqa: E402
from app.core.settings import AppSettings  # noqa: E402
from app.ui import notes_page as notes_module  # noqa: E402
from app.ui.notes_page import NotesPage  # noqa: E402
from app.ui.widgets import ai_edit_dialog  # noqa: E402
from app.ui.widgets.ai_edit_dialog import AiEditDialog  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_notes(tmp_path, monkeypatch):
    """Заметки теста — в своей папке, а не в настоящей базе пользователя."""
    monkeypatch.setattr(store, "database_path", lambda: str(tmp_path / "notes.db"))


class TestRequest:
    def test_sends_instruction_and_text(self, monkeypatch) -> None:
        sent = {}

        def fake_post(path, body=None, params=None):
            sent.update(path=path, body=body)
            return {"text": "  Коротко  "}

        monkeypatch.setattr(transport, "post", fake_post)
        assert ai.edit_text(" Сделай короче ", " Длинный текст ") == "Коротко"
        assert sent == {"path": "/api/ai/edit",
                        "body": {"instruction": "Сделай короче", "text": "Длинный текст"}}

    @pytest.mark.parametrize("instruction, text", [("", "текст"), ("   ", "текст"), ("правь", "  ")])
    def test_empty_input_is_refused_without_a_request(self, monkeypatch, instruction, text) -> None:
        monkeypatch.setattr(transport, "post", lambda *a, **k: pytest.fail("запрос не нужен"))
        with pytest.raises(ServerError):
            ai.edit_text(instruction, text)

    def test_too_long_text_is_refused_locally(self, monkeypatch) -> None:
        monkeypatch.setattr(transport, "post", lambda *a, **k: pytest.fail("запрос не нужен"))
        with pytest.raises(ServerError, match="Выделите часть"):
            ai.edit_text("правь", "а" * (ai.MAX_TEXT + 1))
        with pytest.raises(ServerError, match="Просьба"):
            ai.edit_text("а" * (ai.MAX_INSTRUCTION + 1), "текст")

    def test_missing_login_gets_a_clear_message(self, monkeypatch) -> None:
        def refuse(*args, **kwargs):
            raise AuthError("Вход в систему не выполнен")

        monkeypatch.setattr(transport, "post", refuse)
        with pytest.raises(AuthError, match="войдите в оплаты"):
            ai.edit_text("правь", "текст")

    def test_server_message_passes_through(self, monkeypatch) -> None:
        def refuse(*args, **kwargs):
            raise ServerError("Лимит ИИ на сегодня исчерпан (50 обращений)", 429)

        monkeypatch.setattr(transport, "post", refuse)
        with pytest.raises(ServerError, match="Лимит ИИ"):
            ai.edit_text("правь", "текст")

    def test_empty_answer_is_an_error(self, monkeypatch) -> None:
        monkeypatch.setattr(transport, "post", lambda *a, **k: {"text": "  "})
        with pytest.raises(ServerError):
            ai.edit_text("правь", "текст")


def run_now(monkeypatch) -> None:
    """Фоновая задача выполняется тут же: тест не ждёт потоков."""
    def immediate(function, *args, on_result=None, on_error=None, **kwargs):
        try:
            result = function(*args)
        except Exception as error:  # noqa: BLE001
            if on_error:
                on_error(str(error))
            return
        if on_result:
            on_result(result)

    monkeypatch.setattr(ai_edit_dialog, "run_task", immediate)


class TestDialog:
    def test_result_is_shown_and_accepted(self, app, monkeypatch) -> None:
        run_now(monkeypatch)
        calls = []
        dialog = AiEditDialog("весь текст", "", request=lambda i, t: calls.append((i, t)) or "итог")
        # Без выделения править можно только страницу целиком.
        assert not dialog.selection_radio.isEnabled()
        dialog.instruction.setPlainText("сократи")
        dialog._run()
        assert calls == [("сократи", "весь текст")]
        assert dialog.result == "итог"
        assert dialog.preview.toPlainText() == "итог"
        assert not dialog.replace_button.isHidden()
        dialog._accept_result()
        assert dialog.result == "итог" and not dialog.for_selection

    def test_selection_is_what_gets_sent(self, app, monkeypatch) -> None:
        run_now(monkeypatch)
        calls = []
        dialog = AiEditDialog("весь текст", "кусок", request=lambda i, t: calls.append(t) or "x")
        assert dialog.selection_radio.isChecked()
        dialog.instruction.setPlainText("правь")
        dialog._run()
        assert calls == ["кусок"] and dialog.for_selection

    def test_error_is_shown_and_nothing_to_replace(self, app, monkeypatch) -> None:
        run_now(monkeypatch)

        def refuse(instruction, text):
            raise ServerError("ИИ на сервере не настроен")

        dialog = AiEditDialog("текст", "", request=refuse)
        dialog.instruction.setPlainText("правь")
        dialog._run()
        assert "не настроен" in dialog.status.text()
        assert dialog.replace_button.isHidden() and dialog.result == ""
        assert dialog.run_button.isEnabled()

    def test_empty_request_is_not_sent(self, app, monkeypatch) -> None:
        run_now(monkeypatch)
        dialog = AiEditDialog("текст", "", request=lambda i, t: pytest.fail("запрос не нужен"))
        dialog._run()
        assert "Напишите" in dialog.status.text()

    def test_quick_request_fills_the_field(self, app) -> None:
        dialog = AiEditDialog("текст", "")
        dialog._use_quick("Исправь ошибки")
        assert dialog.instruction.toPlainText() == "Исправь ошибки"


class FakeDialog:
    """Окно, которое «нажало Заменить» с готовым результатом."""

    DialogCode = AiEditDialog.DialogCode
    accept = True
    result = "новый текст"
    for_selection = False
    seen: tuple[str, str] | None = None

    def __init__(self, whole, selection, parent=None):
        type(self).seen = (whole, selection)

    def exec(self):
        return self.DialogCode.Accepted if self.accept else self.DialogCode.Rejected


@pytest.fixture
def page(app, monkeypatch):
    monkeypatch.setattr(notes_module, "AiEditDialog", FakeDialog)
    FakeDialog.accept, FakeDialog.result, FakeDialog.for_selection = True, "новый текст", False
    FakeDialog.seen = None
    monkeypatch.setattr(AppSettings, "save", lambda self: None)
    settings = AppSettings()
    settings.ai_consent = True
    page = NotesPage(settings, lambda text, kind: None)
    page.resize(1100, 700)
    page.show()
    note = store.create_note(None, "Тест", "исходный текст")
    page._reload_tree(select=note.id)
    return page


class TestPage:
    def test_whole_page_is_replaced_and_can_be_restored(self, page) -> None:
        page.ai_edit()
        assert FakeDialog.seen[0] == "исходный текст"
        assert page.editor.markdown() == "новый текст"
        assert page._dirty and not page.undo_ai_button.isHidden()
        page.undo_ai()
        assert page.editor.markdown() == "исходный текст"
        assert page.undo_ai_button.isHidden()

    def test_cancel_changes_nothing(self, page) -> None:
        FakeDialog.accept = False
        page.ai_edit()
        assert page.editor.markdown() == "исходный текст"
        assert page.undo_ai_button.isHidden()

    def test_selection_is_replaced_in_place(self, page) -> None:
        cursor = page.editor.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(len("исходный"), cursor.MoveMode.KeepAnchor)
        page.editor.setTextCursor(cursor)
        FakeDialog.for_selection, FakeDialog.result = True, "новый"
        page.ai_edit()
        assert FakeDialog.seen[1] == "исходный"
        assert page.editor.markdown() == "новый текст"

    def test_snapshot_does_not_follow_to_another_page(self, page) -> None:
        page.ai_edit()
        other = store.create_note(None, "Другая", "чужое")
        page._reload_tree(select=other.id)
        assert page.undo_ai_button.isHidden() and page._before_ai is None

    def test_consent_is_asked_once(self, page, monkeypatch) -> None:
        page.settings.ai_consent = False
        asked = []

        def question(*args, **kwargs):
            asked.append(args[2])
            return QMessageBox.StandardButton.Yes

        monkeypatch.setattr(QMessageBox, "question", question)
        page.ai_edit()
        page.ai_edit()
        assert len(asked) == 1 and page.settings.ai_consent
        assert "Gemini" in asked[0]

    def test_refused_consent_stops_before_the_dialog(self, page, monkeypatch) -> None:
        page.settings.ai_consent = False
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
        page.ai_edit()
        assert FakeDialog.seen is None
        assert not page.settings.ai_consent
        assert page.editor.markdown() == "исходный текст"


def test_consent_is_saved_in_settings(tmp_path) -> None:
    path = str(tmp_path / "settings.json")
    settings = AppSettings.load(path)
    assert settings.ai_consent is False
    settings.ai_consent = True
    settings.save()
    assert AppSettings.load(path).ai_consent is True
