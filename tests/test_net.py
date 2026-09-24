"""Корневые сертификаты для HTTPS.

Проверяется то, из-за чего программа переставала работать у части людей: набор
корней из сборки должен доверяться всегда, а не только там, где системное
хранилище пусто.

Прежняя версия обращалась к `certifi` лишь при пустом системном хранилище. На
Windows оно никогда не пустое, поэтому на самой распространённой системе набор
из сборки не использовался вовсе, и всё зависело от того, успело ли хранилище
машины получить свежие корни Let's Encrypt. У одного человека программа
работала, у другого — «Сервер оплат недоступен».

Сеть здесь не трогается: проверяется состав доверенных корней, а не соединение.
"""
from __future__ import annotations

import ssl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import net

certifi = pytest.importorskip("certifi")


def _fingerprints(context: ssl.SSLContext) -> set[bytes]:
    return {certificate for certificate in context.get_ca_certs(True)}


def test_корни_из_сборки_доверяются():
    """Главная проверка: каждый корень из набора сборки попал в контекст.

    Без этого свежий корень Let's Encrypt приходит только с обновлением
    системы, а до тех пор часть людей не может войти.
    """
    bundled = _fingerprints(ssl.create_default_context(cafile=certifi.where()))

    assert bundled, "набор certifi пуст — проверять нечего"
    assert bundled <= _fingerprints(net._build())


def test_системные_корни_остаются():
    """Набор из сборки добавляется поверх, а не вместо системного хранилища.

    Иначе в сети с антивирусом или прокси, разбирающим HTTPS, перестал бы
    проходить сертификат локального центра: он есть только в системном
    хранилище.
    """
    system = _fingerprints(ssl.create_default_context())
    if not system:
        pytest.skip("системное хранилище пусто — это macOS, проверять нечего")

    assert system <= _fingerprints(net._build())


def test_проверка_сертификата_не_отключена():
    """Обход TLS не должен появиться как способ починить связь.

    С выключенной проверкой токен уходит тому, кто встал посередине, а ошибка
    при этом исчезает — соблазн заметный, поэтому закреплено тестом.
    """
    context = net._build()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_без_certifi_остаются_системные_корни(monkeypatch):
    """Набор не установлен или не прочитался — работаем на системных корнях."""
    monkeypatch.setitem(sys.modules, "certifi", None)

    context = net._build()

    assert context.verify_mode == ssl.CERT_REQUIRED
