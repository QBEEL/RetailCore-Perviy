"""Ящик пользователя: адрес, пароль и серверы mail.ru.

Ящик у каждого свой и хранится на его компьютере: адрес в настройках, пароль в
системном хранилище (см. `secret`). Серверы общие для всех ящиков mail.ru —
`mail.ru`, `inbox.ru`, `list.ru`, `bk.ru` и корпоративные домены, подключённые
к «Почте для бизнеса».
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import secret

if TYPE_CHECKING:
    from ..settings import AppSettings

IMAP_HOST, IMAP_PORT = "imap.mail.ru", 993
SMTP_HOST, SMTP_PORT = "smtp.mail.ru", 465
TIMEOUT = 30

APP_PASSWORD_URL = "https://account.mail.ru/user/2-step-auth/passwords"

_ADDRESS = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


class MailError(Exception):
    """Письмо не отправлено или ящик не прочитан. Текст пригоден для показа."""


def is_address(text: str) -> bool:
    return bool(_ADDRESS.match(text.strip()))


@dataclass(frozen=True)
class Mailbox:
    address: str
    password: str
    name: str = ""


class MailConfig:
    """Связывает настройки программы с ящиком.

    Одна на процесс, как сессия оплат: поле открытия файла лежит глубоко в
    разделах, и протаскивать настройки в каждое из них ради одной кнопки
    незачем.
    """

    def __init__(self) -> None:
        self.settings: AppSettings | None = None

    def bind(self, settings: "AppSettings | None") -> None:
        self.settings = settings

    @property
    def address(self) -> str:
        return self.settings.mail_address.strip() if self.settings else ""

    @property
    def configured(self) -> bool:
        return bool(self.mailbox())

    def mailbox(self) -> Mailbox | None:
        """Ящик с паролем, или `None`, если чего-то из двух нет."""
        if not self.settings or not self.address:
            return None
        password = secret.load(self.address)
        if not password:
            return None
        return Mailbox(self.address, password, self.settings.mail_sender_name.strip())

    def remember_recipients(self, addresses: list[str]) -> None:
        """Недавние получатели: подсказка в следующем письме."""
        if not self.settings:
            return
        known = self.settings.mail_recent_to
        for address in reversed(addresses):
            if address in known:
                known.remove(address)
            known.insert(0, address)
        del known[20:]
        self.settings.save()


config = MailConfig()
