"""Замещение на время отпуска.

Сервер: заместитель правит оплаты отсутствующего, пока идёт срок, и не правит
чужих сверх него; назначить замещение нельзя себе же, задним числом наоборот и
на отключённую учётку. Клиент: вкладка администрирования показывает замещения,
а окно не даёт сохранить бессмыслицу.

Серверные зависимости на рабочем месте не нужны, поэтому без них серверная
часть файла пропускается.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    from server_app.routers import covers  # noqa: PLC0415
    from server_app.schemas import CoverIn  # noqa: PLC0415
    return security, covers, CoverIn


# --- право на правку -----------------------------------------------------------

def test_заместитель_правит_оплаты_отсутствующего():
    security, _, _ = _server()
    user = security.User(id=2, login="b", full_name="Борис", is_admin=False,
                         responsible=frozenset({"Борис"}),
                         covered=frozenset({"Анна"}))

    assert user.may_edit("Борис")
    assert user.may_edit("Анна")
    assert not user.may_edit("Виктор")


def test_без_замещения_чужое_править_нельзя():
    security, _, _ = _server()
    user = security.User(id=2, login="b", full_name="Борис", is_admin=False,
                         responsible=frozenset({"Борис"}))

    assert not user.may_edit("Анна")
    assert user.mine == frozenset({"Борис"})


def test_замещение_читается_на_каждом_запросе(monkeypatch):
    """Токен про замещение не знает: оно приходит из базы вместе с учёткой,
    поэтому снятое замещение перестаёт действовать сразу."""
    security, _, _ = _server()
    seen = {}

    def fetch_one(query, params=None):
        seen["query"] = query
        return {"id": 2, "login": "b", "full_name": "Борис", "is_admin": False,
                "is_active": True, "token_version": 0, "app_version": None,
                "app_platform": None, "app_seen_at": None,
                "responsible": ["Борис"], "covered": ["Анна"]}

    monkeypatch.setattr(security.db, "fetch_one", fetch_one)
    token = security.create_token(2, "b", 0)

    user = security.current_user(token, None, None)

    assert user.covered == frozenset({"Анна"})
    assert user.may_edit("Анна")
    # Срок считается по московской дате и включительно.
    assert "user_cover" in seen["query"]
    assert "BETWEEN c.date_from AND c.date_to" in seen["query"]
    assert "Europe/Moscow" in seen["query"]


# --- назначение ----------------------------------------------------------------

def _people(monkeypatch, covers, *, cover_active=True):
    people = {
        1: {"id": 1, "full_name": "Анна", "login": "a", "is_active": True},
        2: {"id": 2, "full_name": "Борис", "login": "b", "is_active": cover_active},
    }
    monkeypatch.setattr(covers.db, "fetch_one",
                        lambda query, params=None: people.get(params[0]))


def _form(CoverIn, **kw):
    values = dict(absent_id=1, cover_id=2, date_from=date(2026, 11, 2),
                  date_to=date(2026, 11, 15))
    values.update(kw)
    return CoverIn(**values)


def test_назначить_замещение_можно(monkeypatch):
    _, covers, CoverIn = _server()
    _people(monkeypatch, covers)

    absent, cover = covers._check(_form(CoverIn))

    assert absent["full_name"] == "Анна"
    assert cover["full_name"] == "Борис"


@pytest.mark.parametrize("kw, text", [
    ({"cover_id": 1}, "сам себя"),
    ({"date_to": date(2026, 11, 1)}, "раньше начала"),
])
def test_бессмысленное_замещение_отклоняется(monkeypatch, kw, text):
    _, covers, CoverIn = _server()
    from fastapi import HTTPException
    _people(monkeypatch, covers)

    with pytest.raises(HTTPException) as error:
        covers._check(_form(CoverIn, **kw))

    assert error.value.status_code == 422
    assert text in error.value.detail


def test_отключённая_учётка_заместить_не_может(monkeypatch):
    _, covers, CoverIn = _server()
    from fastapi import HTTPException
    _people(monkeypatch, covers, cover_active=False)

    with pytest.raises(HTTPException) as error:
        covers._check(_form(CoverIn))

    assert "отключена" in error.value.detail


def test_замещения_видит_и_меняет_только_администратор():
    security, covers, _ = _server()

    for route in covers.router.routes:
        dependencies = [d.call for d in route.dependant.dependencies]
        assert security.admin_only in dependencies, route.path


# --- приложение ----------------------------------------------------------------

def test_замещения_читаются_из_ответа_сервера():
    from app.core.payments import admin

    cover = admin._cover({
        "id": 5, "absent_id": 1, "absent_name": "Анна", "cover_id": 2,
        "cover_name": "Борис", "date_from": "2026-11-02", "date_to": "2026-11-15",
        "note": "отпуск", "state": "active", "created_by": "Админ"})

    assert cover.date_from == date(2026, 11, 2)
    assert cover.state_title == "идёт"
    assert cover.cover_name == "Борис"


def test_сервер_без_замещений_даёт_пустой_список(monkeypatch):
    """Раздел из-за необновлённого сервера открываться не перестаёт."""
    from app.core.payments import admin, transport

    def refuse(path, params=None):
        raise transport.ServerError("нет такого адреса", 404)

    monkeypatch.setattr(admin.transport, "get", refuse)

    assert admin.covers() == []


def test_замещение_уходит_на_сервер_датами_в_iso(monkeypatch):
    from app.core.payments import admin

    sent = {}

    def post(path, body=None, params=None):
        sent.update(path=path, body=body)
        return {"id": 9, "absent_id": 1, "cover_id": 2, "state": "planned",
                "date_from": body["date_from"], "date_to": body["date_to"]}

    monkeypatch.setattr(admin.transport, "post", post)

    saved = admin.save_cover(admin.Cover(
        absent_id=1, cover_id=2, date_from=date(2026, 11, 2),
        date_to=date(2026, 11, 15), note=" отпуск "))

    assert sent["path"] == "/api/covers"
    assert sent["body"]["date_from"] == "2026-11-02"
    assert sent["body"]["note"] == "отпуск"
    assert saved.id == 9


@pytest.fixture(scope="module")
def application():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_вкладка_показывает_замещения(application):
    from app.core.payments import admin
    from app.core.payments.admin import Cover
    from app.core.settings import AppSettings
    from app.ui.admin_page import AdminPage

    page = AdminPage(AppSettings(), lambda *_: None)
    page._apply(([], [], [], [], [], admin.Scope(), [], [
        Cover(id=1, absent_name="Анна", cover_name="Борис", state="active",
              date_from=date(2026, 11, 2), date_to=date(2026, 11, 15)),
        Cover(id=2, absent_name="Виктор", cover_name="Галина", state="ended",
              date_from=date(2026, 9, 1), date_to=date(2026, 9, 14))]))

    assert page.covers_table.model_.rowCount() == 2
    assert page.tabs.tabText(1) == "Замещения (1)"
    assert "идёт сейчас: 1" in page.covers_hint.text()


def test_окно_не_даёт_сохранить_бессмыслицу(application):
    from app.core.payments.admin import Account
    from app.ui.widgets.cover_dialog import CoverDialog

    accounts = [Account(id=1, login="a", full_name="Анна"),
                Account(id=2, login="b", full_name="Борис"),
                Account(id=3, login="c", full_name="Виктор", is_active=False)]
    dialog = CoverDialog(accounts)

    # Отключённый заместителем не предлагается, а отсутствовать может любой.
    assert dialog.cover_box.findData(3) == -1
    assert dialog.absent.findData(3) >= 0

    dialog.absent.setCurrentIndex(dialog.absent.findData(1))
    dialog.cover_box.setCurrentIndex(dialog.cover_box.findData(1))
    dialog._accept()
    assert "сам себя" in dialog.error.text()

    dialog.cover_box.setCurrentIndex(dialog.cover_box.findData(2))
    dialog.date_from.set_value(date(2026, 11, 10))
    dialog.date_to.set_value(date(2026, 11, 1))
    dialog._accept()
    assert "раньше начала" in dialog.error.text()

    dialog.date_to.set_value(date(2026, 11, 20))
    result = dialog.result_cover()
    assert (result.absent_id, result.cover_id) == (1, 2)
    assert result.date_to == date(2026, 11, 20)
