"""Разбор ссылки `vless://`, которую отдаёт панель 3X-UI.

Ссылка несёт всё, что нужно клиенту: адрес, порт, идентификатор и параметры
транспорта. Человеку не приходится переписывать их по полям, а значит, и
ошибаться в них.

Поддерживается ровно то, что настроено на сервере: VLESS по WebSocket, с TLS
или без. Всё остальное отклоняется понятным сообщением, а не молча
превращается в подключение, которое не заработает.
"""
from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit


class VlessError(ValueError):
    """Ссылку нельзя использовать; текст ошибки показывается человеку."""


@dataclass(frozen=True)
class VlessLink:
    user_id: str
    host: str
    port: int
    tls: bool
    path: str = "/"
    ws_host: str = ""
    sni: str = ""
    fingerprint: str = ""
    alpn: tuple[str, ...] = ()
    name: str = ""
    # Исходный текст ссылки: по нему выбранный из подписки сервер запоминается
    # и разбирается заново при следующем запуске, без обращения к сети.
    raw: str = ""

    @property
    def server_name(self) -> str:
        """Имя для проверки сертификата: SNI, иначе заголовок Host, иначе адрес.

        Адрес-число в качестве имени сертификата не годится, поэтому он берётся
        последним и только если других имён нет.
        """
        return self.sni or self.ws_host or self.host

    @property
    def request_host(self) -> str:
        """Значение заголовка Host в рукопожатии WebSocket."""
        return self.ws_host or self.sni or self.host


def parse(text: str) -> VlessLink:
    raw = text.strip()
    if not raw:
        raise VlessError("Вставьте ссылку vless:// из панели 3X-UI.")
    parts = urlsplit(raw)
    if parts.scheme.lower() != "vless":
        raise VlessError("Ссылка должна начинаться с vless://")

    try:
        user_id = str(uuid.UUID(unquote(parts.username or "")))
    except ValueError:
        raise VlessError("В ссылке нет корректного идентификатора пользователя (UUID).") from None
    host = parts.hostname or ""
    if not host:
        raise VlessError("В ссылке не указан адрес сервера.")
    try:
        port = parts.port
    except ValueError:
        port = None
    if not port:
        raise VlessError("В ссылке не указан порт сервера.")

    query = {key: values[-1] for key, values in parse_qs(parts.query, keep_blank_values=True).items()}
    if query.get("encryption", "none") not in ("none", ""):
        raise VlessError("Шифрование VLESS (encryption) не поддерживается: нужна ссылка с encryption=none.")
    if query.get("flow"):
        raise VlessError("Параметр flow относится к TCP и Reality; для WebSocket он не нужен.")
    if query.get("type", "tcp") != "ws":
        raise VlessError(
            f"Транспорт «{query.get('type', 'tcp')}» не поддерживается: нужен VLESS по WebSocket (type=ws).")

    security = query.get("security", "none")
    if security not in ("none", "tls"):
        raise VlessError(f"Защита «{security}» не поддерживается: нужна none или tls.")
    # Отключённая проверка сертификата отдаёт весь трафик тому, кто встал между
    # компьютером и сервером, а ошибка при этом исчезает. Такой ссылке не верим.
    if query.get("allowInsecure", "0") not in ("0", "", "false"):
        raise VlessError(
            "Ссылка требует отключить проверку сертификата (allowInsecure) — это не поддерживается. "
            "Выпустите сертификат для домена сервера.")

    path = unquote(query.get("path", "")) or "/"
    if not path.startswith("/"):
        path = "/" + path
    alpn = tuple(item for item in unquote(query.get("alpn", "")).split(",") if item)
    return VlessLink(
        user_id=user_id,
        host=host,
        port=port,
        tls=security == "tls",
        path=path,
        ws_host=unquote(query.get("host", "")),
        sni=unquote(query.get("sni", "")),
        fingerprint=query.get("fp", ""),
        alpn=alpn,
        name=unquote(parts.fragment),
        raw=raw,
    )


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True
