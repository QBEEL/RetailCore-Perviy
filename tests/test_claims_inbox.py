"""Торг-2, сервер: что считается «ждёт ответа КМ» в уведомлении.

Заглянуть в карточку мало — напоминание висит, пока КМ не принял заявку в
работу. Серверные зависимости на рабочем месте не нужны, поэтому без них файл
пропускается.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

SERVER_APP = Path(__file__).resolve().parents[1] / "server" / "api" / "app"


def _server():
    pytest.importorskip("fastapi")
    pytest.importorskip("jwt")
    pytest.importorskip("psycopg_pool")
    os.environ.setdefault("API_SECRET", "x" * 40)
    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
    if "server_app" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "server_app", SERVER_APP / "__init__.py",
            submodule_search_locations=[str(SERVER_APP)])
        package = importlib.util.module_from_spec(spec)
        sys.modules["server_app"] = package
        spec.loader.exec_module(package)
    from server_app import security  # noqa: PLC0415
    from server_app.routers import claims  # noqa: PLC0415
    return security, claims


def test_уведомление_висит_до_принятия_в_работу(monkeypatch):
    security, claims = _server()
    seen: dict[str, tuple] = {}

    def fetch_one(query, params=None):
        seen["count"] = (query, params)
        return {"unseen": 2, "open": 5}

    def fetch_all(query, params=None):
        seen["items"] = (query, params)
        return [{"id": 9, "kind": "defect", "brand": "Rink", "product": "Крем",
                 "applicant": "Мария"}]

    monkeypatch.setattr(claims.db, "fetch_one", fetch_one)
    monkeypatch.setattr(claims.db, "fetch_all", fetch_all)
    user = security.User(id=7, login="km", full_name="КМ", is_admin=False,
                         responsible=frozenset())

    answer = claims.inbox(user)

    assert (answer.unseen, answer.open) == (2, 5)
    assert answer.items[0].id == 9
    for key in ("count", "items"):
        query, params = seen[key]
        # Ждёт ответа: новая — пока не принята, а не пока не открыта; вернувшаяся
        # из архива — пока о ней не узнали. Закрытые и заведённые КМ на себя — нет.
        assert "c.status = 'new' OR NOT c.manager_seen" in query
        assert "c.status <> 'closed'" in query
        assert "c.applicant_id <> c.manager_id" in query
        assert params[0] == 7

