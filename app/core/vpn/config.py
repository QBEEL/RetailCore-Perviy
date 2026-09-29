"""Конфигурация sing-box по разобранной ссылке.

Внутри — один входящий прокси на локальном адресе и один выходящий канал на
сервер. Что пускать напрямую, решает не sing-box, а системная настройка прокси
(список исключений в `sysproxy`): браузер сам не отдаёт ему адреса, которые
должны идти мимо туннеля.
"""
from __future__ import annotations

from typing import Any

from .vless_uri import VlessLink, is_ip

LISTEN_HOST = "127.0.0.1"


def build(link: VlessLink, port: int, log_file: str) -> dict[str, Any]:
    outbound: dict[str, Any] = {
        "type": "vless",
        "tag": "proxy",
        "server": link.host,
        "server_port": link.port,
        "uuid": link.user_id,
        "transport": {
            "type": "ws",
            "path": link.path,
            "headers": {"Host": link.request_host},
        },
    }
    if link.tls:
        tls: dict[str, Any] = {"enabled": True}
        if not is_ip(link.server_name):
            tls["server_name"] = link.server_name
        if link.alpn:
            tls["alpn"] = list(link.alpn)
        if link.fingerprint:
            tls["utls"] = {"enabled": True, "fingerprint": link.fingerprint}
        outbound["tls"] = tls

    return {
        "log": {"level": "warn", "output": log_file, "timestamp": True},
        "dns": {"servers": [{"type": "local", "tag": "local"}]},
        "inbounds": [{
            "type": "mixed",
            "tag": "in",
            "listen": LISTEN_HOST,
            "listen_port": port,
        }],
        "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
        "route": {"final": "proxy", "default_domain_resolver": "local"},
    }
