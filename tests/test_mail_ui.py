"""Почта в интерфейсе: окно письма, входящие, настройки, кнопка в поле файла."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from app.core import mail  # noqa: E402
from app.core.mail import secret  # noqa: E402
from app.core.settings import AppSettings  # noqa: E402
from app.ui.settings_page import SettingsPage  # noqa: E402
from app.ui.widgets import mail_dialogs  # noqa: E402
from app.ui.widgets.file_picker import FilePicker  # noqa: E402
from app.ui.widgets.mail_dialogs import InboxDialog, SendMailDialog  # noqa: E402
from app.ui.widgets.toast import ToastKind  # noqa: E402

BOX = mail.Mailbox("buyer@mail.ru", "pw", "Иван Петров")


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def clean_mail_state(tmp_path: Path):
    secret._memory.clear()
    settings = AppSettings(_path=str(tmp_path / "settings.json"))
    mail.config.bind(settings)
    yield settings
    mail.config.bind(None)
    secret._memory.clear()


def wait_for(condition, timeout: float = 5.0) -> None:
    """Результат фоновой задачи приходит в цикл событий — крутим его, пока не придёт."""
    import time

    end = time.monotonic() + timeout
    while not condition() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)
    assert condition(), "фоновая задача не завершилась"


@pytest.fixture
def report(tmp_path: Path) -> str:
    path = tmp_path / "Отчёт.xlsx"
    path.write_bytes(b"PK")
    return str(path)


# --- письмо -------------------------------------------------------------------

def test_dialog_sends_and_remembers_recipients(application, report, monkeypatch,
                                               clean_mail_state) -> None:
    sent = []
    monkeypatch.setattr(mail, "send_message", lambda box, message: sent.append(message))
    dialog = SendMailDialog(BOX, [report], subject="Отчёт за сентябрь")
    dialog.to_field.setText("sup@x.ru; boss@x.ru")

    dialog.send_button.click()
    wait_for(lambda: dialog.result() == QDialog.DialogCode.Accepted)

    assert len(sent) == 1
    assert sent[0]["To"] == "sup@x.ru, boss@x.ru"
    assert [a.get_filename() for a in sent[0].iter_attachments()] == ["Отчёт.xlsx"]
    assert clean_mail_state.mail_recent_to == ["sup@x.ru", "boss@x.ru"]


def test_dialog_signs_letter_with_sender_name(application, report) -> None:
    dialog = SendMailDialog(BOX, [report])
    assert dialog.body_field.toPlainText().endswith("С уважением,\nИван Петров")


def test_dialog_rejects_bad_address_without_sending(application, report, monkeypatch) -> None:
    monkeypatch.setattr(mail, "send_message", lambda *_: pytest.fail("письмо не должно уйти"))
    dialog = SendMailDialog(BOX, [report])
    dialog.to_field.setText("не адрес")

    dialog.send_button.click()

    assert not dialog.message.isHidden()
    assert "не похоже на адрес" in dialog.message.text()
    assert dialog.result() != QDialog.DialogCode.Accepted


def test_dialog_keeps_letter_when_server_refuses(application, report, monkeypatch) -> None:
    def refuse(box, message):
        raise mail.MailError("Почта не приняла пароль.")

    monkeypatch.setattr(mail, "send_message", refuse)
    dialog = SendMailDialog(BOX, [report])
    dialog.to_field.setText("sup@x.ru")
    dialog.body_field.setPlainText("Важный текст")

    dialog.send_button.click()
    wait_for(lambda: not dialog.message.isHidden())

    assert dialog.message.text() == "Почта не приняла пароль."
    assert dialog.send_button.isEnabled(), "после ошибки письмо можно отправить снова"
    assert dialog.body_field.toPlainText() == "Важный текст"


def test_send_without_mailbox_explains_how_to_connect(application, report, monkeypatch) -> None:
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "information",
                        lambda parent, title, text, *rest: shown.append(text))

    assert mail_dialogs.send_files(None, [report]) is False
    assert "Настройки" in shown[0]


# --- входящие -----------------------------------------------------------------

def _attachment(name: str, uid: str = "1") -> mail.Attachment:
    return mail.Attachment(uid, 0, name, 2048, "Поставщик", "Счёт на оплату",
                           datetime(2026, 10, 20, 10, 30))


def test_inbox_lists_files_and_downloads_chosen(application, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mail, "list_attachments",
                        lambda box, days, limit, progress=None: [
                            _attachment("новый.pdf", "2"), _attachment("старый.xlsx", "1")])
    saved: list[tuple[str, str]] = []

    def save(box, item, folder):
        saved.append((item.filename, folder))
        return str(tmp_path / item.filename)

    monkeypatch.setattr(mail, "save_attachment", save)
    monkeypatch.setattr(mail_dialogs, "download_folder", lambda: str(tmp_path))

    dialog = InboxDialog(BOX)
    wait_for(lambda: dialog.table.rowCount() == 2)
    assert dialog.table.item(0, 3).text() == "новый.pdf"
    assert dialog.table.item(0, 0).text() == "20.10.2026 10:30"
    assert not dialog.choose_button.isEnabled(), "пока строка не выбрана, скачивать нечего"

    dialog.table.selectRow(1)
    assert dialog.choose_button.isEnabled()
    dialog.choose_button.click()
    wait_for(lambda: dialog.result() == QDialog.DialogCode.Accepted)

    assert saved == [("старый.xlsx", str(tmp_path))]
    assert dialog.chosen_path == str(tmp_path / "старый.xlsx")


def test_inbox_reports_login_failure(application, monkeypatch) -> None:
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda parent, title, text, *rest: shown.append(text))

    def refuse(box, days, limit, progress=None):
        raise mail.MailError("Нет связи с почтой.")

    monkeypatch.setattr(mail, "list_attachments", refuse)
    dialog = InboxDialog(BOX)
    wait_for(lambda: bool(shown))

    assert shown == ["Нет связи с почтой."]
    assert dialog.refresh_button.isEnabled()


def test_picker_takes_file_from_mail(application, monkeypatch, tmp_path) -> None:
    book = tmp_path / "price.xlsx"
    book.write_bytes(b"PK")
    monkeypatch.setattr(mail_dialogs, "pick_from_mail", lambda parent: str(book))
    picker = FilePicker("Прайс")
    chosen: list[str] = []
    picker.file_selected.connect(chosen.append)

    picker.mail_button.click()

    assert chosen == [str(book)]


def test_picker_ignores_closed_mail_window(application, monkeypatch) -> None:
    monkeypatch.setattr(mail_dialogs, "pick_from_mail", lambda parent: "")
    picker = FilePicker("Прайс")
    chosen: list[str] = []
    picker.file_selected.connect(chosen.append)

    picker.mail_button.click()

    assert chosen == [] and picker.path == ""


def test_picker_hides_mail_button_until_mailbox_is_connected(application,
                                                             clean_mail_state) -> None:
    picker = FilePicker("Прайс")
    picker.show()
    assert picker.mail_button.isHidden(), "ящика нет — и кнопки нет"

    clean_mail_state.mail_address = "buyer@mail.ru"
    picker.refresh_mail_button()
    assert picker.mail_button.isHidden(), "адрес без пароля — ящик не подключён"

    secret._memory["buyer@mail.ru"] = "pw"
    picker.refresh_mail_button()
    assert not picker.mail_button.isHidden()

    secret.forget("buyer@mail.ru")
    picker.hide()
    picker.show()
    assert picker.mail_button.isHidden(), "после «Забыть пароль» кнопка исчезает при показе"


# --- настройки ----------------------------------------------------------------

def _page(settings: AppSettings) -> tuple[SettingsPage, list[tuple[str, ToastKind]]]:
    toasts: list[tuple[str, ToastKind]] = []
    return SettingsPage(settings, lambda text, kind: toasts.append((text, kind))), toasts


def test_settings_saves_mailbox_only_after_successful_login(application, monkeypatch,
                                                            clean_mail_state) -> None:
    checked: list[mail.Mailbox] = []
    monkeypatch.setattr(mail, "verify", checked.append)
    page, toasts = _page(clean_mail_state)
    page.mail_address_field.setText("buyer@mail.ru")
    page.mail_name_field.setText("Иван Петров")
    page.mail_password_field.setText("app-pass")

    page.mail_save_button.click()
    wait_for(lambda: clean_mail_state.mail_address == "buyer@mail.ru")

    assert checked == [mail.Mailbox("buyer@mail.ru", "app-pass", "Иван Петров")]
    assert secret.load("buyer@mail.ru") == "app-pass"
    assert page.mail_password_field.text() == "", "введённый пароль с экрана убирается"
    assert mail.config.configured
    assert toasts[-1] == ("Почта подключена", ToastKind.SUCCESS)


def test_settings_does_not_keep_rejected_password(application, monkeypatch,
                                                  clean_mail_state) -> None:
    def refuse(box):
        raise mail.MailError("Почта не приняла пароль.")

    monkeypatch.setattr(mail, "verify", refuse)
    page, toasts = _page(clean_mail_state)
    page.mail_address_field.setText("buyer@mail.ru")
    page.mail_password_field.setText("wrong")

    page.mail_save_button.click()
    wait_for(lambda: bool(toasts))

    assert toasts[-1] == ("Почта не приняла пароль.", ToastKind.ERROR)
    assert clean_mail_state.mail_address == ""
    assert secret.load("buyer@mail.ru") == ""
    assert page.mail_save_button.isEnabled()


def test_settings_asks_for_valid_address_and_password(application, monkeypatch,
                                                      clean_mail_state) -> None:
    monkeypatch.setattr(mail, "verify", lambda box: pytest.fail("проверять нечего"))
    page, toasts = _page(clean_mail_state)

    page.mail_address_field.setText("без собаки")
    page.mail_save_button.click()
    page.mail_address_field.setText("buyer@mail.ru")
    page.mail_save_button.click()

    assert [kind for _, kind in toasts] == [ToastKind.WARNING, ToastKind.WARNING]
    assert "адрес" in toasts[0][0] and "пароль" in toasts[1][0]


def test_settings_forget_removes_password_but_keeps_address(application,
                                                            clean_mail_state) -> None:
    clean_mail_state.mail_address = "buyer@mail.ru"
    secret.save("buyer@mail.ru", "pw")
    page, _ = _page(clean_mail_state)
    assert page.mail_forget_button.isEnabled()

    page.mail_forget_button.click()

    assert secret.load("buyer@mail.ru") == ""
    assert clean_mail_state.mail_address == "buyer@mail.ru"
    assert not page.mail_forget_button.isEnabled()
    assert not mail.config.configured
