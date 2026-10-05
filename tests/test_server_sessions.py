"""Входы на сервере: версия учётки, смена пароля, предел продления.

Сервер — отдельный пакет со своими зависимостями (FastAPI, PyJWT, PostgreSQL).
Здесь он загружается под другим именем, чтобы не столкнуться с клиентским
`app`, а база подменяется заглушкой, помнящей одну учётку. Без FastAPI и PyJWT
тесты пропускаются.
"""
from __future__ import annotations

import importlib
import sys
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("jwt")
pytest.importorskip("fastapi")

import jwt  # noqa: E402
from fastapi import HTTPException  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "server" / "api" / "app"
SECRET = "s" * 40
NAME = "retail_server"


class FakeDb:
    """Одна учётка и журнал запросов."""

    def __init__(self, password_hash: str) -> None:
        self.user = {
            "id": 5, "login": "ivanov", "full_name": "Иванов", "is_admin": False,
            "is_active": True, "token_version": 0, "responsible": [],
            "must_change_password": False, "password_hash": password_hash,
        }
        self.executed: list[tuple[str, object]] = []

    def fetch_one(self, query: str, params=None):
        if query.lstrip().startswith("UPDATE"):
            self.user["token_version"] += 1
            self.user["must_change_password"] = False
            return dict(self.user)
        if "password_hash FROM" in query:
            return {"password_hash": self.user["password_hash"]}
        return dict(self.user)

    def fetch_all(self, query: str, params=None):
        return []

    def execute(self, query: str, params=None):
        self.executed.append((query, params))
        return 1


@pytest.fixture
def server():
    package = types.ModuleType(NAME)
    package.__path__ = [str(ROOT)]
    routers = types.ModuleType(f"{NAME}.routers")
    routers.__path__ = [str(ROOT / "routers")]
    config = types.ModuleType(f"{NAME}.settings")
    config.settings = types.SimpleNamespace(
        secret=SECRET, token_hours=12, session_days=7, files_dir="/tmp", dsn="")
    stub = types.ModuleType(f"{NAME}.db")
    before = set(sys.modules)
    sys.modules.update({NAME: package, f"{NAME}.routers": routers,
                        f"{NAME}.settings": config, f"{NAME}.db": stub})
    security = importlib.import_module(f"{NAME}.security")
    fake = FakeDb(security.hash_password("старый-пароль"))
    for name in ("fetch_one", "fetch_all", "execute"):
        setattr(stub, name, getattr(fake, name))
    modules = types.SimpleNamespace(
        security=security,
        auth=importlib.import_module(f"{NAME}.routers.auth"),
        users=importlib.import_module(f"{NAME}.routers.users"),
        schemas=importlib.import_module(f"{NAME}.schemas"),
        db=fake)
    yield modules
    for name in set(sys.modules) - before:
        sys.modules.pop(name, None)


def _token(server, **claims) -> str:
    payload = {"sub": "5", "login": "ivanov", "ver": 0, "auth": int(time.time()),
               "iat": int(time.time()), "exp": int(time.time()) + 3600, **claims}
    payload = {key: value for key, value in payload.items() if value is not None}
    return jwt.encode(payload, SECRET, algorithm="HS256")


def test_токен_текущей_версии_принимается(server):
    assert server.security.current_user(_token(server)).login == "ivanov"


def test_токен_без_версии_читается_как_нулевая(server):
    """Выданный до обновления сервера: сразу после него никого не выбрасывает."""
    legacy = _token(server, ver=None, auth=None)
    assert server.security.current_user(legacy).id == 5


def test_вход_прежней_версии_заканчивается(server):
    old = _token(server)
    server.db.user["token_version"] = 1
    with pytest.raises(HTTPException) as denied:
        server.security.current_user(old)
    assert denied.value.status_code == 401


def test_отключённая_учётка_не_входит(server):
    server.db.user["is_active"] = False
    with pytest.raises(HTTPException):
        server.security.current_user(_token(server))


def test_смена_пароля_заканчивает_прежние_входы_и_выдаёт_новый(server):
    old = _token(server)
    user = server.security.current_user(old)
    form = server.schemas.PasswordChange(old_password="старый-пароль",
                                         new_password="новый-пароль-1")

    answer = server.auth.change_password(form, user)

    assert server.db.user["token_version"] == 1
    with pytest.raises(HTTPException):
        server.security.current_user(old)
    assert server.security.current_user(answer.access_token).login == "ivanov"


def test_неверный_старый_пароль_входы_не_меняет(server):
    user = server.security.current_user(_token(server))
    form = server.schemas.PasswordChange(old_password="не тот", new_password="новый-пароль-1")
    with pytest.raises(HTTPException) as denied:
        server.auth.change_password(form, user)
    assert denied.value.status_code == 403
    assert server.db.user["token_version"] == 0


def test_продление_сохраняет_момент_входа(server):
    entered = int(time.time()) - 3 * 86400
    user = server.security.current_user(_token(server, auth=entered))

    renewed = server.auth.refresh(user)

    claims = jwt.decode(renewed.access_token, SECRET, algorithms=["HS256"])
    assert claims["auth"] == entered and claims["ver"] == 0


def test_продлевать_после_предела_сессии_нельзя(server):
    user = server.security.current_user(_token(server, auth=int(time.time()) - 8 * 86400))
    with pytest.raises(HTTPException) as denied:
        server.auth.refresh(user)
    assert denied.value.status_code == 401


def test_сброс_пароля_администратором_поднимает_версию(server):
    admin = server.security.User(id=1, login="admin", full_name="", is_admin=True,
                                 responsible=frozenset())
    server.users.reset_password(5, admin)
    update = next(query for query, _ in server.db.executed if query.lstrip().startswith("UPDATE"))
    assert "token_version = token_version + 1" in update
