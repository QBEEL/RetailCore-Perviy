"""Почта: пароль, сборка письма, отправка и вложения из входящих.

Настоящий ящик не нужен: серверы SMTP и IMAP подменены, а письма собраны здесь.
"""
from __future__ import annotations

import imaplib
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

from app.core import mail
from app.core.mail import account, inbox, secret
from app.core.settings import AppSettings

BOX = mail.Mailbox("buyer@mail.ru", "app-password", "Иван Петров")


@pytest.fixture(autouse=True)
def fresh_secret_memory():
    secret._memory.clear()
    yield
    secret._memory.clear()


# --- пароль -------------------------------------------------------------------

@pytest.mark.skipif(not sys.platform.startswith("win"), reason="DPAPI есть только в Windows")
def test_password_is_encrypted_on_disk_and_read_back() -> None:
    secret.save("Buyer@mail.ru", "секрет-123")
    on_disk = Path(secret._file_path()).read_text(encoding="utf-8")
    assert "секрет-123" not in on_disk

    secret._memory.clear()
    assert secret.load("buyer@mail.ru") == "секрет-123"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="DPAPI есть только в Windows")
def test_forget_removes_password_from_disk() -> None:
    secret.save("buyer@mail.ru", "secret")
    secret.forget("buyer@mail.ru")
    assert secret.load("buyer@mail.ru") == ""
    assert "buyer@mail.ru" not in Path(secret._file_path()).read_text(encoding="utf-8")


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="DPAPI есть только в Windows")
def test_password_copied_from_another_machine_is_ignored() -> None:
    path = Path(secret._file_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"buyer@mail.ru": "bm90IGRwYXBp"}', encoding="utf-8")
    assert secret.load("buyer@mail.ru") == ""


def test_mailbox_needs_both_address_and_password() -> None:
    settings = AppSettings(_path="unused.json")
    config = account.MailConfig()
    config.bind(settings)
    assert config.mailbox() is None

    settings.mail_address = "buyer@mail.ru"
    assert config.mailbox() is None, "без пароля ящик не готов"

    secret._memory["buyer@mail.ru"] = "pw"
    settings.mail_sender_name = "Иван"
    assert config.mailbox() == mail.Mailbox("buyer@mail.ru", "pw", "Иван")


def test_password_never_reaches_settings_file(tmp_path: Path) -> None:
    settings = AppSettings(_path=str(tmp_path / "settings.json"))
    settings.mail_address = "buyer@mail.ru"
    secret._memory["buyer@mail.ru"] = "super-secret"
    settings.save()
    text = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "buyer@mail.ru" in text and "super-secret" not in text

    again = AppSettings.load(str(tmp_path / "settings.json"))
    assert again.mail_address == "buyer@mail.ru"


def test_recent_recipients_keep_order_without_duplicates(tmp_path: Path) -> None:
    settings = AppSettings(_path=str(tmp_path / "settings.json"))
    config = account.MailConfig()
    config.bind(settings)
    config.remember_recipients(["a@x.ru", "b@x.ru"])
    config.remember_recipients(["b@x.ru", "c@x.ru"])
    assert settings.mail_recent_to == ["b@x.ru", "c@x.ru", "a@x.ru"]


# --- письмо -------------------------------------------------------------------

def test_recipients_split_on_any_separator() -> None:
    assert mail.parse_recipients("a@x.ru, b@x.ru; c@x.ru\nA@X.RU") == [
        "a@x.ru", "b@x.ru", "c@x.ru"]


@pytest.mark.parametrize("text", ["a@x", "не адрес", "a@@x.ru"])
def test_bad_recipient_is_explained(text: str) -> None:
    with pytest.raises(mail.MailError, match="не похоже на адрес"):
        mail.parse_recipients(text)


def test_message_carries_cyrillic_attachment_and_sender_name(tmp_path: Path) -> None:
    report = tmp_path / "Отчёт за сентябрь.xlsx"
    report.write_bytes(b"PK-data")
    message = mail.build_message(BOX, ["sup@x.ru"], "Отчёт", "Добрый день!", [str(report)])

    assert "buyer@mail.ru" in message["From"]
    assert message["To"] == "sup@x.ru"
    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == "Отчёт за сентябрь.xlsx"
    assert attachment.get_payload(decode=True) == b"PK-data"
    assert message.get_body().get_content().strip() == "Добрый день!"


def test_copy_to_self_goes_in_hidden_copy() -> None:
    message = mail.build_message(BOX, ["sup@x.ru"], "Тема", "", [], copy_to_self=True)
    assert message["Bcc"] == "buyer@mail.ru"
    assert "buyer@mail.ru" not in message["To"]


def test_no_recipient_is_refused() -> None:
    with pytest.raises(mail.MailError, match="кому отправить"):
        mail.build_message(BOX, [], "Тема", "", [])


def test_oversized_attachments_are_refused_before_sending(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail.send, "MAX_ATTACHMENTS_BYTES", 10)
    big = tmp_path / "big.xlsx"
    big.write_bytes(b"x" * 11)
    with pytest.raises(mail.MailError, match="17 МБ"):
        mail.build_message(BOX, ["a@x.ru"], "Тема", "", [str(big)])


def test_missing_attachment_is_explained(tmp_path: Path) -> None:
    with pytest.raises(mail.MailError, match="Не удалось прочитать файл"):
        mail.build_message(BOX, ["a@x.ru"], "Тема", "", [str(tmp_path / "нет.xlsx")])


class FakeSmtp:
    """SMTP, запоминающий, что в него положили."""

    sent: list[EmailMessage] = []
    login_error: Exception | None = None
    send_error: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None) -> None:
        self.host, self.port = host, port

    def login(self, user, password) -> None:
        if self.login_error:
            raise self.login_error
        self.credentials = (user, password)

    def send_message(self, message) -> None:
        if self.send_error:
            raise self.send_error
        self.sent.append(message)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        pass


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch):
    FakeSmtp.sent, FakeSmtp.login_error, FakeSmtp.send_error = [], None, None
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    return FakeSmtp


def test_message_is_sent_through_mail_ru(smtp) -> None:
    message = mail.build_message(BOX, ["sup@x.ru"], "Тема", "Текст", [])
    mail.send_message(BOX, message)
    assert smtp.sent == [message]


def test_wrong_password_points_to_app_passwords(smtp) -> None:
    smtp.login_error = smtplib.SMTPAuthenticationError(535, b"bad")
    with pytest.raises(mail.MailError, match="пароль для внешнего приложения"):
        mail.send_message(BOX, mail.build_message(BOX, ["sup@x.ru"], "Тема", "", []))


def test_refused_recipient_is_named(smtp) -> None:
    smtp.send_error = smtplib.SMTPRecipientsRefused({"oops@x.ru": (550, b"no user")})
    with pytest.raises(mail.MailError, match="oops@x.ru"):
        mail.send_message(BOX, mail.build_message(BOX, ["oops@x.ru"], "Тема", "", []))


def test_network_failure_is_not_a_traceback(smtp) -> None:
    smtp.send_error = ConnectionResetError("reset")
    with pytest.raises(mail.MailError, match="Нет связи"):
        mail.send_message(BOX, mail.build_message(BOX, ["sup@x.ru"], "Тема", "", []))


# --- входящие -----------------------------------------------------------------

def _letter(subject: str, files: dict[str, bytes], date: str = "Tue, 20 Oct 2026 10:00:00 +0300"
            ) -> bytes:
    message = EmailMessage()
    message["From"] = "Поставщик <supplier@firm.ru>"
    message["To"] = "buyer@mail.ru"
    message["Subject"] = subject
    message["Date"] = date
    message.set_content("Во вложении.")
    for name, data in files.items():
        message.add_attachment(data, maintype="application", subtype="octet-stream",
                               filename=name)
    return message.as_bytes()


def _newsletter() -> bytes:
    message = EmailMessage()
    message["Subject"] = "Newsletter-marker"
    message.set_content("текст")
    message.add_alternative("<p>html</p>", subtype="html")
    return message.as_bytes()


class FakeImap:
    mailbox: dict[bytes, bytes] = {}
    fetched_full: list[bytes] = []
    login_error: Exception | None = None

    def __init__(self, host, port, ssl_context=None, timeout=None) -> None:
        self.host = host
        self.closed = False

    def login(self, user, password) -> None:
        if self.login_error:
            raise self.login_error

    def select(self, name, readonly=False):
        assert readonly, "ящик должен открываться только на чтение"
        return "OK", [b"3"]

    def logout(self) -> None:
        self.closed = True

    def uid(self, command, *arguments):
        if command == "SEARCH":
            return "OK", [b" ".join(self.mailbox)]
        uid, what = arguments
        raw = self.mailbox[uid if isinstance(uid, bytes) else uid.encode()]
        if "HEADER.FIELDS" in what:
            head = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
            return "OK", [(b"1 (RFC822.SIZE %d BODY[HEADER.FIELDS] {%d}" % (len(raw), len(head)),
                           head), b")"]
        assert "PEEK" in what, "письмо нельзя помечать прочитанным"
        FakeImap.fetched_full.append(raw)
        return "OK", [(b"1 (BODY[] {%d}" % len(raw), raw), b")"]


@pytest.fixture
def imap(monkeypatch: pytest.MonkeyPatch):
    FakeImap.mailbox = {
        b"11": _letter("Счёт 15", {"Счёт 15.pdf": b"%PDF-1", "печать.png": b"png"},
                       "Mon, 19 Oct 2026 09:00:00 +0300"),
        b"12": _newsletter(),
        b"13": _letter("Прайс", {"price.xlsx": b"PK-xlsx", "setup.exe": b"MZ"},
                       "Tue, 20 Oct 2026 16:30:00 +0300"),
    }
    FakeImap.fetched_full, FakeImap.login_error = [], None
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    return FakeImap


def test_only_supported_files_are_listed_newest_first(imap) -> None:
    found = mail.list_attachments(BOX)
    assert [(a.filename, a.subject) for a in found] == [
        ("price.xlsx", "Прайс"), ("Счёт 15.pdf", "Счёт 15")]
    assert found[0].sender == "Поставщик"
    assert found[1].size == len(b"%PDF-1")


def test_newsletters_are_not_downloaded(imap) -> None:
    mail.list_attachments(BOX)
    assert all(b"Newsletter-marker" not in raw for raw in imap.fetched_full)


def test_attachment_is_saved_under_its_own_name(imap, tmp_path: Path) -> None:
    item = next(a for a in mail.list_attachments(BOX) if a.filename == "price.xlsx")
    path = mail.save_attachment(BOX, item, str(tmp_path))
    assert Path(path).name == "price.xlsx"
    assert Path(path).read_bytes() == b"PK-xlsx"

    second = mail.save_attachment(BOX, item, str(tmp_path))
    assert Path(second).name == "price (2).xlsx", "чужой файл не перезаписывается"


def test_save_refuses_when_letter_changed(imap, tmp_path: Path) -> None:
    item = next(a for a in mail.list_attachments(BOX) if a.filename == "price.xlsx")
    imap.mailbox[b"13"] = _letter("Прайс", {"другой.xlsx": b"x"})
    with pytest.raises(mail.MailError, match="изменилось"):
        mail.save_attachment(BOX, item, str(tmp_path))


def test_wrong_password_on_login(imap) -> None:
    imap.login_error = imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Authentication failed")
    with pytest.raises(mail.MailError, match="пароль для внешнего приложения"):
        mail.list_attachments(BOX)


def test_progress_reports_every_letter(imap) -> None:
    steps: list[tuple[int, int]] = []
    mail.list_attachments(BOX, progress=lambda done, total: steps.append((done, total)))
    assert steps == [(1, 3), (2, 3), (3, 3)]


def test_mailbox_is_closed_after_use(imap, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[FakeImap] = []
    original = FakeImap.__init__

    def remember(self, *args, **kwargs) -> None:
        original(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(FakeImap, "__init__", remember)
    mail.list_attachments(BOX)
    assert opened and all(box.closed for box in opened)
