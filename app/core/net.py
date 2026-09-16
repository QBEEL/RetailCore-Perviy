"""Доверенные корневые сертификаты для HTTPS.

На Windows `ssl` берёт корни из системного хранилища, и контекст по умолчанию
работает сам собой. На macOS такого моста нет: `ssl` читает только пути,
зашитые в OpenSSL при сборке Python. В приложении, собранном PyInstaller, этих
путей на чужой машине не существует — список корней оказывается пустым, любой
запрос падает с CERTIFICATE_VERIFY_FAILED, а пользователь видит «Сервер
недоступен»: ошибка проверки сертификата приходит тем же `ssl.SSLError`, что и
обрыв связи.

Поэтому контекст создаётся один раз здесь: если системных корней не нашлось,
берём набор из `certifi`, который PyInstaller кладёт внутрь сборки.
"""
from __future__ import annotations

import ssl


def _build() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context
    try:
        import certifi
    except ImportError:
        # Проверку сертификата не отключаем даже здесь: честная ошибка лучше
        # тихого обхода TLS, при котором токен уйдёт кому угодно.
        return context
    return ssl.create_default_context(cafile=certifi.where())


context = _build()
