"""Отправка письма с вложениями через SMTP mail.ru."""
from __future__ import annotations

import mimetypes
import os
import re
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from .. import net
from .account import SMTP_HOST, SMTP_PORT, TIMEOUT, MailError, Mailbox, is_address

# Почта mail.ru принимает письма до 25 МБ вместе с кодированием вложений, а оно
# раздувает файлы на треть. Запас снимает отказ после долгой отправки.
MAX_ATTACHMENTS_BYTES = 17 * 1024 * 1024

_SPLIT = re.compile(r"[,;\s]+")

WRONG_PASSWORD = (
    "Почта не приняла пароль. Нужен не пароль от ящика, а «пароль для внешнего "
    "приложения»: его создают в настройках mail.ru, в разделе «Безопасность».")


def parse_recipients(text: str) -> list[str]:
    """Адреса через запятую, точку с запятой или пробел. Неверный — ошибка."""
    found: list[str] = []
    for part in _SPLIT.split(text.strip()):
        if not part:
            continue
        if not is_address(part):
            raise MailError(f"«{part}» не похоже на адрес электронной почты.")
        if part.lower() not in (item.lower() for item in found):
            found.append(part)
    return found


def build_message(box: Mailbox, to: list[str], subject: str, body: str,
                  attachments: list[str], copy_to_self: bool = False) -> EmailMessage:
    if not to:
        raise MailError("Укажите, кому отправить письмо.")
    message = EmailMessage()
    message["From"] = formataddr((box.name, box.address)) if box.name else box.address
    message["To"] = ", ".join(to)
    if copy_to_self:
        message["Bcc"] = box.address
    message["Subject"] = subject.strip() or "(без темы)"
    message.set_content(body or "")

    total = 0
    for path in attachments:
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as error:
            raise MailError(f"Не удалось прочитать файл «{os.path.basename(path)}»: "
                            f"{error.strerror or error}") from error
        total += len(data)
        if total > MAX_ATTACHMENTS_BYTES:
            raise MailError(
                "Вложения больше 17 МБ — почта такое письмо не примет. "
                "Уберите часть файлов или отправьте их отдельными письмами.")
        kind, encoding = mimetypes.guess_type(path)
        main, _, sub = (kind or "application/octet-stream").partition("/")
        if encoding:
            main, sub = "application", "octet-stream"
        message.add_attachment(data, maintype=main, subtype=sub,
                               filename=os.path.basename(path))
    return message


def send(box: Mailbox, message: EmailMessage) -> None:
    """Отправляет готовое письмо. Каждая ошибка приходит как `MailError`."""
    try:
        with _connect(box) as server:
            server.send_message(message)
    except smtplib.SMTPRecipientsRefused as error:
        refused = ", ".join(error.recipients) or "получатель"
        raise MailError(f"Почта не приняла адрес: {refused}. Проверьте, нет ли опечатки.") from error
    except smtplib.SMTPSenderRefused as error:
        raise MailError("Почта не разрешила отправку с этого адреса. "
                        "Проверьте адрес ящика в настройках.") from error
    except smtplib.SMTPDataError as error:
        raise MailError(f"Почта отклонила письмо: {_reason(error)}") from error
    except MailError:
        raise
    except (smtplib.SMTPException, OSError) as error:
        raise MailError(_network_text(error)) from error


def verify(box: Mailbox) -> None:
    """Входит на сервер отправки и выходит: письмо не уходит."""
    try:
        with _connect(box):
            pass
    except MailError:
        raise
    except (smtplib.SMTPException, OSError) as error:
        raise MailError(_network_text(error)) from error


def _connect(box: Mailbox) -> smtplib.SMTP_SSL:
    server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT, context=net.context)
    try:
        server.login(box.address, box.password)
    except smtplib.SMTPAuthenticationError as error:
        server.close()
        raise MailError(WRONG_PASSWORD) from error
    except BaseException:
        server.close()
        raise
    return server


def _reason(error: smtplib.SMTPResponseException) -> str:
    text = error.smtp_error
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return f"{text.strip()} (код {error.smtp_code})"


def _network_text(error: Exception) -> str:
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "Почта не отвечает. Проверьте связь и повторите."
    if isinstance(error, ssl.SSLError):
        return ("Не удалось установить защищённое соединение с почтой: "
                f"{error.reason or error}. Антивирус или прокси могут подменять сертификат.")
    if isinstance(error, (socket.gaierror, ConnectionError, OSError)):
        return f"Нет связи с почтой ({SMTP_HOST}). Проверьте подключение к интернету."
    return f"Ошибка отправки: {error}"
