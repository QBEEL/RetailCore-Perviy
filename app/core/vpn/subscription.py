"""Ссылка подписки 3X-UI: список серверов вместо одной ссылки.

Подписка отдаёт текст, чаще всего в base64, по одной ссылке в строке. В ней
бывают серверы других протоколов (Hysteria2 и прочие): клиент их не умеет, и
они пропускаются, а не ломают весь список.

Адрес подписки содержит ключ доступа, поэтому запрос идёт только по HTTPS:
по открытому каналу ключ ушёл бы каждому, кто стоит на пути.
"""
from __future__ import annotations

import base64
import binascii
import re
import urllib.request
from urllib.parse import urlsplit

from .. import net
from .vless_uri import VlessError, VlessLink, parse

MAX_BYTES = 1_000_000
# «FI» отдельным словом («FI-4», «| FI |»), но не внутри «FIX» или «CONFIG».
_FINLAND = re.compile(r"(?<![A-Z])FI(?![A-Z])|FINLAND|🇫🇮")


def is_subscription(text: str) -> bool:
    return text.strip().lower().startswith(("https://", "http://"))


def decode(payload: bytes) -> list[VlessLink]:
    """Серверы, которые умеет клиент, из тела ответа подписки."""
    raw = payload.strip()
    try:
        text = base64.b64decode(raw + b"=" * (-len(raw) % 4), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        text = raw.decode("utf-8", "replace")

    links: list[VlessLink] = []
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            links.append(parse(line))
        except VlessError:
            skipped += 1
    if not links:
        raise VlessError(
            "В подписке нет серверов, которые поддерживает программа (нужен VLESS по WebSocket)."
            + (f" Пропущено других: {skipped}." if skipped else ""))
    return links


def fetch(url: str, timeout: float = 15.0) -> list[VlessLink]:
    """Скачивает подписку и возвращает пригодные серверы."""
    address = url.strip()
    parts = urlsplit(address)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise VlessError("Ссылка подписки должна начинаться с https://")
    request = urllib.request.Request(address, headers={"User-Agent": "RetailCore"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=net.context) as response:
            payload = response.read(MAX_BYTES + 1)
    except OSError as error:
        raise VlessError(f"Подписка не загрузилась: {error}") from error
    if len(payload) > MAX_BYTES:
        raise VlessError("Подписка слишком большая — это не список серверов.")
    return decode(payload)


def preferred(links: list[VlessLink], remembered: str = "") -> VlessLink:
    """Сервер по умолчанию: прежний выбор, иначе финский, иначе первый.

    Финский угадывается по флагу или по «FI»/«Finland» в названии: так они
    называются у панели, а другого признака в подписке нет.
    """
    for link in links:
        if remembered and link.name == remembered:
            return link
    for link in links:
        if _FINLAND.search(link.name.upper()):
            return link
    return links[0]
