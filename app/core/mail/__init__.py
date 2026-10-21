"""Почта: отправка писем с файлами и вложения из входящих (mail.ru)."""
from . import inbox, secret, send
from .account import APP_PASSWORD_URL, MailError, Mailbox, config, is_address
from .inbox import Attachment, list_attachments, save_attachment
from .send import build_message, parse_recipients
from .send import send as send_message


def verify(box: Mailbox) -> None:
    """Проверяет вход и на отправку, и на чтение. Ошибка — `MailError`."""
    send.verify(box)
    inbox.verify(box)


__all__ = [
    "APP_PASSWORD_URL", "Attachment", "MailError", "Mailbox", "build_message", "config",
    "inbox", "is_address", "list_attachments", "parse_recipients", "save_attachment",
    "secret", "send", "send_message", "verify",
]
