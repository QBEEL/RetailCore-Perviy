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
from app.ui.widgets.usage_meter import UsageMeter  # noqa: E402


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


def usage_of(used=3, limit=20, reset=3 * 3600 + 120, **extra) -> ai.Usage:
    return ai.Usage(used=used, limit=limit, reset_seconds=reset, **extra)


class TestUsage:
    def test_numbers(self) -> None:
        usage = usage_of(used=5, limit=20)
        assert usage.remaining == 15 and usage.available and usage.ratio == 0.25
        assert usage_of(used=25, limit=20).remaining == 0
        assert usage_of(used=20, limit=20).ratio == 1.0 and not usage_of(used=20, limit=20).available

    def test_zero_limit_is_closed(self) -> None:
        usage = usage_of(used=0, limit=0)
        assert usage.closed and not usage.available and usage.ratio == 1.0

    def test_not_configured_is_unavailable(self) -> None:
        assert not usage_of(configured=False).available

    def test_read_from_server_answer(self, monkeypatch) -> None:
        monkeypatch.setattr(transport, "get", lambda path, params=None: {
            "used": 4, "limit": 10, "reset_seconds": 90, "max_text": 5000,
            "max_instruction": 200, "configured": True})
        usage = ai.usage()
        assert (usage.used, usage.limit, usage.max_text, usage.max_instruction) == (4, 10, 5000, 200)

    def test_reset_wording(self) -> None:
        assert ai.reset_in(5 * 3600 + 12 * 60) == "через 5 ч 12 мин"
        assert ai.reset_in(2 * 3600) == "через 2 ч"
        assert ai.reset_in(40 * 60) == "через 40 мин"
        assert ai.reset_in(10) == "через 1 мин"


class TestMeter:
    def test_colour_follows_how_full_it_is(self, app) -> None:
        meter = UsageMeter("ИИ")
        meter.show_usage(usage_of(used=5, limit=20))
        assert meter.tone == "ok"
        meter.show_usage(usage_of(used=15, limit=20))
        assert meter.tone == "warn"
        meter.show_usage(usage_of(used=19, limit=20))
        assert meter.tone == "danger"

    def test_text_says_how_many_are_left(self, app) -> None:
        meter = UsageMeter("ИИ")
        meter.show_usage(usage_of(used=6, limit=20))
        assert meter.value.text() == "осталось 14 из 20"
        assert "через 3 ч 02 мин" in meter.note.text()
        compact = UsageMeter("ИИ", compact=True)
        compact.show_usage(usage_of(used=6, limit=20))
        assert compact.value.text() == "6 из 20" and "полночь" in compact.toolTip()

    def test_spent_limit_says_when_it_returns(self, app) -> None:
        meter = UsageMeter("ИИ")
        meter.show_usage(usage_of(used=20, limit=20))
        assert meter.tone == "danger" and "исчерпан" in meter.note.text()
        assert meter.track.shown == 1.0

    def test_closed_and_unconfigured_show_words_not_numbers(self, app) -> None:
        meter = UsageMeter("ИИ")
        meter.show_usage(usage_of(used=0, limit=0))
        assert meter.value.text() == "закрыт" and meter.tone == "muted" and meter.track.shown == 0
        meter.show_usage(usage_of(configured=False))
        assert meter.value.text() == "не настроен"

    def test_text_size(self, app) -> None:
        meter = UsageMeter("Размер")
        meter.show_size(3400, 20000)
        assert meter.value.text() == "3 400 из 20 000 знаков" and meter.tone == "ok"
        meter.show_size(25000, 20000)
        assert meter.tone == "danger" and "5 000" in meter.note.text()


class TestDialogLimits:
    def test_meters_show_the_limit_and_the_size(self, app, monkeypatch) -> None:
        run_now(monkeypatch)
        dialog = AiEditDialog("а" * 3000, "", usage=usage_of(used=3, limit=20),
                              load_usage=lambda: usage_of(used=3, limit=20))
        assert dialog.usage_meter.value.text() == "осталось 17 из 20"
        assert dialog.size_meter.value.text() == "3 000 из 20 000 знаков"
        assert dialog.run_button.isEnabled()

    def test_spent_limit_blocks_running(self, app, monkeypatch) -> None:
        run_now(monkeypatch)
        spent = usage_of(used=20, limit=20)
        dialog = AiEditDialog("текст", "", usage=spent, load_usage=lambda: spent,
                              request=lambda i, t: pytest.fail("запрос не нужен"))
        assert not dialog.run_button.isEnabled()
        assert "исчерпан" in dialog.status.text()
        dialog.instruction.setPlainText("правь")
        dialog._run()

    def test_closed_account_blocks_running(self, app, monkeypatch) -> None:
        run_now(monkeypatch)
        closed = usage_of(used=0, limit=0)
        dialog = AiEditDialog("текст", "", usage=closed, load_usage=lambda: closed)
        assert not dialog.run_button.isEnabled() and "закрыл" in dialog.status.text()

    def test_too_long_page_blocks_until_a_fragment_is_chosen(self, app, monkeypatch) -> None:
        run_now(monkeypatch)
        fine = usage_of()
        dialog = AiEditDialog("а" * 20001, "кусок", usage=fine, load_usage=lambda: fine)
        # Есть выделенное — оно и правится, а оно короткое.
        assert dialog.run_button.isEnabled()
        dialog.whole_radio.setChecked(True)
        assert not dialog.run_button.isEnabled() and dialog.size_meter.tone == "danger"
        dialog.selection_radio.setChecked(True)
        assert dialog.run_button.isEnabled() and dialog.status.text() == ""

    def test_meter_is_refreshed_after_a_run(self, app, monkeypatch) -> None:
        run_now(monkeypatch)
        states = iter([usage_of(used=3), usage_of(used=4)])
        dialog = AiEditDialog("текст", "", load_usage=lambda: next(states),
                              request=lambda i, t: "итог")
        assert dialog.usage_meter.value.text() == "осталось 17 из 20"
        dialog.instruction.setPlainText("правь")
        dialog._run()
        assert dialog.usage_meter.value.text() == "осталось 16 из 20"

    def test_server_error_is_not_wiped_by_the_refresh(self, app, monkeypatch) -> None:
        run_now(monkeypatch)

        def refuse(instruction, text):
            raise ServerError("ИИ вернул ошибку. Попробуйте ещё раз")

        dialog = AiEditDialog("текст", "", load_usage=lambda: usage_of(), request=refuse)
        dialog.instruction.setPlainText("правь")
        dialog._run()
        assert "ИИ вернул ошибку" in dialog.status.text()

    def test_unreadable_usage_does_not_block(self, app, monkeypatch) -> None:
        run_now(monkeypatch)

        def broken():
            raise ServerError("Сервер оплат недоступен")

        dialog = AiEditDialog("текст", "", load_usage=broken)
        assert dialog.usage_meter.value.text() == "нет данных"
        assert dialog.run_button.isEnabled()

    def test_instruction_is_cut_to_the_limit(self, app, monkeypatch) -> None:
        run_now(monkeypatch)
        dialog = AiEditDialog("текст", "", usage=usage_of(max_instruction=10),
                              load_usage=lambda: usage_of(max_instruction=10))
        dialog.instruction.setPlainText("а" * 40)
        assert len(dialog.instruction.toPlainText()) == 10


class TestAdmin:
    def test_account_carries_the_limit(self, monkeypatch) -> None:
        from app.core.payments import admin

        account = admin._account({
            "id": 5, "login": "km", "full_name": "КМ", "responsible": [], "is_admin": False,
            "is_active": True, "created_at": None, "ai_limit": 0, "ai_used": 3})
        assert account.ai_limit == 0 and account.ai_used == 3
        common = admin._account({"id": 6, "login": "x", "full_name": "", "responsible": [],
                                 "is_admin": False, "is_active": True, "ai_limit": None})
        assert common.ai_limit is None and common.ai_used == 0

    def test_limit_is_sent_on_save(self, monkeypatch) -> None:
        from app.core.payments import admin

        sent = {}

        def fake_put(path, body=None):
            sent.update(body)
            return {"id": 5, "login": "km", "full_name": "КМ", "responsible": [],
                    "is_admin": False, "is_active": True, "ai_limit": body["ai_limit"]}

        monkeypatch.setattr(transport, "put", fake_put)
        saved = admin.save(admin.Account(id=5, login="km", full_name="КМ", ai_limit=7))
        assert sent["ai_limit"] == 7 and saved.ai_limit == 7
        admin.save(admin.Account(id=5, login="km", full_name="КМ", ai_limit=None))
        assert sent["ai_limit"] is None

    def test_dialog_edits_the_limit(self, app) -> None:
        from app.core.payments.admin import Account
        from app.ui.widgets.account_dialogs import AccountDialog

        dialog = AccountDialog(Account(id=5, login="km", full_name="КМ", ai_used=4),
                               known_responsible=[])
        # Не отмечено — предел общий, поле недоступно.
        assert not dialog.ai_limit.isEnabled() and dialog.result_account().ai_limit is None
        assert "общий предел" in dialog.ai_note.text() and "Сегодня использовано: 4" in dialog.ai_note.text()
        dialog.ai_own.setChecked(True)
        dialog.ai_limit.setValue(35)
        assert dialog.result_account().ai_limit == 35
        dialog.ai_limit.setValue(0)
        assert dialog.result_account().ai_limit == 0 and "закрывает" in dialog.ai_note.text()

    def test_dialog_shows_an_existing_limit(self, app) -> None:
        from app.core.payments.admin import Account
        from app.ui.widgets.account_dialogs import AccountDialog

        dialog = AccountDialog(Account(id=5, login="km", full_name="КМ", ai_limit=12),
                               known_responsible=[])
        assert dialog.ai_own.isChecked() and dialog.ai_limit.value() == 12

    def test_table_text(self) -> None:
        from app.core.payments.admin import Account
        from app.ui.admin_page import _ai_usage_text

        assert _ai_usage_text(Account(ai_limit=None)) == "общий"
        assert _ai_usage_text(Account(ai_limit=None, ai_used=3)) == "3 (общий)"
        assert _ai_usage_text(Account(ai_limit=0)) == "закрыто"
        assert _ai_usage_text(Account(ai_limit=20, ai_used=7)) == "7 из 20"


class FakeDialog:
    """Окно, которое «нажало Заменить» с готовым результатом."""

    DialogCode = AiEditDialog.DialogCode
    accept = True
    result = "новый текст"
    for_selection = False
    seen: tuple[str, str] | None = None

    def __init__(self, whole, selection, parent=None, usage=None):
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
