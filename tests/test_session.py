"""Обязательный вход: сохранённый токен и льготный срок без сервера.

Проверяется то, ради чего всё затевалось: пароль не спрашивают на каждый
запуск, пароль нигде не хранится, а недоступный сервер не останавливает работу
с файлами тем, кто входил недавно.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import appdata
from app.core.payments import session_store, transport


@pytest.fixture
def profile(monkeypatch, tmp_path):
    """Профиль приложения в отдельной папке — рядом с настоящим не ходим."""
    monkeypatch.setattr(appdata, "data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(appdata, "path_to",
                        lambda name: str(tmp_path / name))
    return tmp_path


def _session(hours: float = 12.0) -> transport.Session:
    return transport.Session(
        base_url="https://retail.test", token="токен", login="e.ivanov",
        user_id=5, full_name="Иванов Евгений", is_admin=True,
        responsible=("Иванов Евгений",), directions=("beauty",),
        expires_at=datetime.now() + timedelta(hours=hours))


# --- сохранение входа -------------------------------------------------------------

def test_вход_переживает_перезапуск(profile):
    """Программу закрывают и открывают весь день — пароль спрашивают раз."""
    session_store.save(_session())

    saved = session_store.load()
    assert saved.valid
    assert saved.login == "e.ivanov" and saved.user_id == 5
    assert saved.directions == ("beauty",)


def test_пароль_не_сохраняется(profile):
    """В файле профиля не должно быть ничего, чем можно войти повторно."""
    session_store.save(_session())

    raw = json.loads((profile / session_store.FILE_NAME).read_text(encoding="utf-8"))
    assert "password" not in raw
    assert not any("парол" in str(key).lower() for key in raw)


def test_истёкший_токен_не_считается_входом(profile):
    session_store.save(_session(hours=-1))

    assert not session_store.load().valid


def test_токен_на_исходе_считается_истёкшим(profile):
    """Иначе первый же запрос вернул бы 401 вместо ожидаемых данных."""
    session_store.save(_session(hours=0.001))

    assert not session_store.load().valid


def test_испорченный_файл_равен_его_отсутствию(profile):
    (profile / session_store.FILE_NAME).write_text("{не json", encoding="utf-8")

    saved = session_store.load()
    assert not saved.valid and not saved.known


# --- льготный срок ------------------------------------------------------------------

def test_после_входа_есть_неделя_без_сервера(profile):
    session_store.save(_session())

    assert session_store.load().grace_left() == session_store.GRACE_DAYS


def test_льготный_срок_истекает(profile, monkeypatch):
    session_store.save(_session())
    saved = session_store.load()
    saved.confirmed_at = datetime.now() - timedelta(days=session_store.GRACE_DAYS + 1)

    assert saved.grace_left() == 0


def test_кто_не_входил_никогда_льготы_не_имеет(profile):
    """Первый запуск на новой машине обязан требовать пароль."""
    saved = session_store.load()

    assert not saved.known
    assert saved.grace_left() == 0


def test_выход_убирает_токен_но_помнит_вход(profile):
    """Льготный срок не должен обнуляться оттого, что человек вышел сам."""
    session_store.save(_session())

    session_store.forget()
    saved = session_store.load()

    assert not saved.token and not saved.valid
    assert saved.known and saved.grace_left() == session_store.GRACE_DAYS
    assert saved.login == "e.ivanov"


# --- восстановление сессии ----------------------------------------------------------

def test_сессия_поднимается_из_токена(profile):
    """Вход по сохранённому токену не спрашивает сервер и не ждёт ответа."""
    transport.session.clear()
    session_store.save(_session())

    transport.restore(session_store.load())

    assert transport.session.active
    assert transport.session.user_id == 5
    assert transport.session.is_admin
    assert transport.session.directions == ("beauty",)
    transport.session.clear()


def test_восстановленная_сессия_не_требует_смены_пароля(profile):
    """Требование снимается заменой и переживает перезапуск на сервере.

    Если бы флаг восстанавливался из файла, человек, уже сменивший пароль,
    получал бы окно замены при каждом запуске.
    """
    transport.session.clear()
    session_store.save(_session())
    transport.session.must_change_password = True

    transport.restore(session_store.load())

    assert not transport.session.must_change_password
    transport.session.clear()
