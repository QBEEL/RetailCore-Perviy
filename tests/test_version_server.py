"""Сервер запоминает версию приложения у учётки.

Серверные зависимости на рабочем месте не нужны — без них файл пропускается.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("jwt")
pytest.importorskip("psycopg_pool")

SERVER_APP = Path(__file__).resolve().parents[1] / "server" / "api" / "app"


def _load():
    os.environ.setdefault("API_SECRET", "x" * 40)
    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
    if "server_app" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "server_app", SERVER_APP / "__init__.py", submodule_search_locations=[str(SERVER_APP)])
        package = importlib.util.module_from_spec(spec)
        sys.modules["server_app"] = package
        spec.loader.exec_module(package)
    from server_app import security  # noqa: PLC0415
    return security


security = _load()

NOW = datetime.now(timezone.utc)


class Recorder:
    def __init__(self) -> None:
        self.writes: list[tuple] = []

    def execute(self, query, params=None):
        self.writes.append(params)
        return 1


@pytest.fixture
def writes(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(security.db, "execute", recorder.execute)
    return recorder


def row(**extra) -> dict:
    return {"id": 7, "app_version": None, "app_platform": None, "app_seen_at": None, **extra}


class TestNoteClient:
    def test_first_contact_is_recorded(self, writes) -> None:
        security.note_client(row(), "3.9.2", "windows")
        assert writes.writes == [("3.9.2", "windows", 7)]

    def test_nothing_sent_means_nothing_recorded(self, writes) -> None:
        security.note_client(row(), None, None)
        security.note_client(row(), "", "windows")
        assert writes.writes == []

    @pytest.mark.parametrize("junk", ["abc", "3.9", "3.9.2.1", "3.9.x", "'; DROP TABLE app_user;--",
                                      "1" * 40, "3.9.2\n"[:0] + "3 .9.2"])
    def test_junk_version_is_ignored(self, writes, junk) -> None:
        security.note_client(row(), junk, "windows")
        assert writes.writes == []

    def test_unknown_platform_is_dropped_but_version_kept(self, writes) -> None:
        security.note_client(row(), "3.9.2", "plan9")
        assert writes.writes == [("3.9.2", "", 7)]

    def test_same_version_seen_recently_is_not_rewritten(self, writes) -> None:
        recent = row(app_version="3.9.2", app_platform="windows",
                     app_seen_at=NOW - timedelta(minutes=10))
        security.note_client(recent, "3.9.2", "windows")
        assert writes.writes == []

    def test_same_version_is_refreshed_after_an_hour(self, writes) -> None:
        stale = row(app_version="3.9.2", app_platform="windows",
                    app_seen_at=NOW - timedelta(hours=2))
        security.note_client(stale, "3.9.2", "windows")
        assert writes.writes == [("3.9.2", "windows", 7)]

    def test_new_version_is_written_at_once(self, writes) -> None:
        recent = row(app_version="3.9.1", app_platform="windows",
                     app_seen_at=NOW - timedelta(minutes=1))
        security.note_client(recent, "3.9.2", "windows")
        assert writes.writes == [("3.9.2", "windows", 7)]

    def test_database_failure_does_not_break_the_request(self, monkeypatch) -> None:
        def broken(query, params=None):
            raise RuntimeError("база недоступна")

        monkeypatch.setattr(security.db, "execute", broken)
        security.note_client(row(), "3.9.2", "windows")


class TestCurrentUser:
    def test_header_is_picked_up_with_the_token(self, monkeypatch, writes) -> None:
        token = security.create_token(7, "km", 0)
        monkeypatch.setattr(security.db, "fetch_one", lambda q, p=None: {
            "id": 7, "login": "km", "full_name": "КМ", "is_admin": False, "is_active": True,
            "token_version": 0, "responsible": [], "covered": [], "app_version": None,
            "app_platform": None, "app_seen_at": None})
        user = security.current_user(token, "3.9.2", "macos")
        assert user.id == 7 and writes.writes == [("3.9.2", "macos", 7)]

    def test_no_header_is_still_a_normal_request(self, monkeypatch, writes) -> None:
        token = security.create_token(7, "km", 0)
        monkeypatch.setattr(security.db, "fetch_one", lambda q, p=None: {
            "id": 7, "login": "km", "full_name": "КМ", "is_admin": False, "is_active": True,
            "token_version": 0, "responsible": [], "covered": [], "app_version": None,
            "app_platform": None, "app_seen_at": None})
        assert security.current_user(token, None, None).login == "km"
        assert writes.writes == []

    def test_disabled_account_records_nothing(self, monkeypatch, writes) -> None:
        token = security.create_token(7, "km", 0)
        monkeypatch.setattr(security.db, "fetch_one", lambda q, p=None: {
            "id": 7, "login": "km", "full_name": "КМ", "is_admin": False, "is_active": False,
            "token_version": 0, "responsible": [], "covered": [], "app_version": None,
            "app_platform": None, "app_seen_at": None})
        with pytest.raises(Exception):
            security.current_user(token, "3.9.2", "windows")
        assert writes.writes == []
