"""Вложения из входящих: счета и прайсы поставщиков, пришедшие на почту.

Письма читаются без отметки «прочитано» (`BODY.PEEK`, ящик открыт только на
чтение): программа смотрит почту, а не разбирает её за человека.

Скачивать все письма за две недели ради пары счетов слишком дорого, поэтому
работа идёт в два шага. Сначала у каждого письма берутся только заголовки и
размер. Вложение бывает лишь у письма, чей тип — `multipart/mixed` или файл
целиком, а текстовые письма и рассылки (`multipart/alternative`) отсеиваются
сразу. Целиком тянутся только оставшиеся.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

from .. import net
from ..payments.safe_files import is_risky, safe_name
from .account import IMAP_HOST, IMAP_PORT, TIMEOUT, MailError, Mailbox
from .send import WRONG_PASSWORD

# Что программа умеет открыть: книги Excel, счёт PDF и выгрузка 1С.
EXTENSIONS = (".pdf", ".xlsx", ".xlsm", ".xls", ".csv")
# Письмо крупнее не берём: в нём либо архив, либо сканы, которые программе не нужны.
MAX_MESSAGE_BYTES = 25 * 1024 * 1024

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
           "Nov", "Dec")
_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")
_HEADERS = "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE CONTENT-TYPE)])"


@dataclass(frozen=True)
class Attachment:
    """Файл из письма. `uid` и `position` однозначно находят его на сервере."""

    uid: str
    position: int
    filename: str
    size: int
    sender: str
    subject: str
    received: datetime | None


def list_attachments(box: Mailbox, days: int = 14, limit: int = 40,
                     progress: Callable[[int, int], None] | None = None
                     ) -> list[Attachment]:
    """Вложения подходящих типов из писем за последние дни, свежие сверху."""
    found: list[Attachment] = []
    with _Session(box) as server:
        uids = _recent_uids(server, days)[-limit:]
        for done, uid in enumerate(uids, start=1):
            if progress:
                progress(done, len(uids))
            found.extend(_attachments_of(server, uid))
    found.sort(key=lambda item: item.received or datetime.min, reverse=True)
    return found


def save_attachment(box: Mailbox, item: Attachment, folder: str) -> str:
    """Скачивает файл в папку и возвращает путь. Имя занято — добавляется номер."""
    changed = "Письмо на сервере изменилось, файл не найден. Обновите список."
    with _Session(box) as server:
        message = _message(server, item.uid)
        for position, part in _files(message):
            if position == item.position:
                break
        else:
            raise MailError(changed)
        name = safe_name(part.get_filename(), "вложение")
        if name != item.filename or is_risky(name):
            raise MailError(changed)
        data = part.get_payload(decode=True) or b""
    os.makedirs(folder, exist_ok=True)
    path = _free_path(folder, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def verify(box: Mailbox) -> None:
    """Входит в ящик и выходит."""
    with _Session(box):
        pass


class _Session:
    """Вход в ящик на чтение. Выход — при закрытии, что бы ни случилось."""

    def __init__(self, box: Mailbox) -> None:
        self._box = box
        self.imap: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> imaplib.IMAP4_SSL:
        try:
            self.imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=net.context,
                                          timeout=TIMEOUT)
            self.imap.login(self._box.address, self._box.password)
            status, _ = self.imap.select("INBOX", readonly=True)
            if status != "OK":
                raise MailError("Не удалось открыть папку «Входящие».")
        except imaplib.IMAP4.error as error:
            self._close()
            raise MailError(_login_text(error)) from error
        except (OSError, EOFError) as error:
            self._close()
            raise MailError(_network_text(error)) from error
        except BaseException:
            self._close()
            raise
        return self.imap

    def __exit__(self, *_exc: object) -> None:
        self._close()

    def _close(self) -> None:
        if self.imap is None:
            return
        try:
            self.imap.logout()
        except (imaplib.IMAP4.error, OSError, EOFError):
            pass
        self.imap = None


def _recent_uids(server: imaplib.IMAP4_SSL, days: int) -> list[bytes]:
    # Название месяца в команде — всегда английское, в обход языка системы.
    since = date.today() - timedelta(days=days)
    stamp = f"{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}"
    status, data = _call(server, "SEARCH", None, "SINCE", stamp)
    return data[0].split() if status == "OK" and data and data[0] else []


def _attachments_of(server: imaplib.IMAP4_SSL, uid: bytes) -> list[Attachment]:
    status, data = _call(server, "FETCH", uid, _HEADERS)
    head = _first_literal(data) if status == "OK" else None
    if head is None:
        return []
    size_match = _SIZE.search(head[0])
    if size_match and int(size_match.group(1)) > MAX_MESSAGE_BYTES:
        return []
    headers = email.message_from_bytes(head[1], policy=policy.default)
    kind = str(headers.get("Content-Type", "")).split(";")[0].strip().lower()
    if not (kind.startswith("application/")
            or (kind.startswith("multipart/") and kind != "multipart/alternative")):
        return []

    message = _message(server, uid.decode())
    sender = _sender(message)
    subject = str(message.get("Subject", "")).strip()
    received = _received(message)
    return [
        Attachment(uid.decode(), position, name, len(part.get_payload(decode=True) or b""),
                   sender, subject, received)
        for position, part in _files(message)
        if (name := safe_name(part.get_filename(), ""))
        and name.lower().endswith(EXTENSIONS) and not is_risky(name)
    ]


def _message(server: imaplib.IMAP4_SSL, uid: str) -> EmailMessage:
    status, data = _call(server, "FETCH", uid, "(BODY.PEEK[])")
    found = _first_literal(data) if status == "OK" else None
    if found is None:
        raise MailError("Письмо не удалось прочитать: возможно, его уже удалили.")
    return email.message_from_bytes(found[1], policy=policy.default)  # type: ignore[return-value]


def _files(message: EmailMessage):  # type: ignore[no-untyped-def]
    """Части письма, у которых есть имя файла, в порядке обхода."""
    position = 0
    for part in message.walk():
        if part.is_multipart() or not part.get_filename():
            continue
        yield position, part
        position += 1


def _call(server: imaplib.IMAP4_SSL, command: str, *arguments):  # type: ignore[no-untyped-def]
    try:
        return server.uid(command, *arguments)
    except imaplib.IMAP4.error as error:
        raise MailError(f"Почта отказала в запросе: {error}") from error
    except (OSError, EOFError) as error:
        raise MailError(_network_text(error)) from error


def _first_literal(data: list) -> tuple[bytes, bytes] | None:
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            return item[0], item[1]
    return None


def _sender(message: EmailMessage) -> str:
    name, address = parseaddr(str(message.get("From", "")))
    return name or address


def _received(message: EmailMessage) -> datetime | None:
    try:
        moment = parsedate_to_datetime(str(message.get("Date", "")))
    except (TypeError, ValueError):
        return None
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def _free_path(folder: str, name: str) -> str:
    root, extension = os.path.splitext(name)
    path, number = os.path.join(folder, name), 1
    while os.path.exists(path):
        number += 1
        path = os.path.join(folder, f"{root} ({number}){extension}")
    return path


def _login_text(error: Exception) -> str:
    text = str(error)
    if "authenticationfailed" in text.lower().replace(" ", "") or "login" in text.lower():
        return (WRONG_PASSWORD + " Для чтения почты при создании пароля выбирают "
                "доступ «Почтовые клиенты».")
    return f"Почта отказала во входе: {text}"


def _network_text(error: Exception) -> str:
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "Почта не отвечает. Проверьте связь и повторите."
    if isinstance(error, ssl.SSLError):
        return ("Не удалось установить защищённое соединение с почтой: "
                f"{error.reason or error}. Антивирус или прокси могут подменять сертификат.")
    return f"Нет связи с почтой ({IMAP_HOST}). Проверьте подключение к интернету."
