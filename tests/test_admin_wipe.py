"""Удаление данных общей базы администратором.

Сеть не трогается: транспорт подменяется, и проверяется то, из-за чего это
действие вообще опасно, — что случайно оно не запускается. Слово подтверждения
и пароль должны уходить на сервер вместе с запросом, кнопка — оставаться
неактивной, пока набрано не то, а сервер прежней версии не должен ломать
раздел администрирования только потому, что удалять через него нечем.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.payments import admin, transport


@pytest.fixture
def calls(monkeypatch):
    """Перехват запросов к серверу: что ушло и что вернулось."""
    log: list[tuple[str, str, object]] = []
    answers: dict[str, object] = {}

    def get(path, params=None):
        log.append(("GET", path, params))
        answer = answers.get(path)
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else {}

    def post(path, body=None, params=None):
        log.append(("POST", path, body))
        answer = answers.get(path)
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else {}

    monkeypatch.setattr(transport, "get", get)
    monkeypatch.setattr(transport, "post", post)
    monkeypatch.setattr(transport.session, "base_url", "https://retail.test")
    monkeypatch.setattr(transport.session, "token", "тестовый")
    return log, answers


_FULL = {"payments": 6958, "files": 214, "budgets": 12,
         "imports": 43, "links": 317}


# --- объём базы ----------------------------------------------------------------

def test_объём_базы_читается_с_сервера(calls):
    log, answers = calls
    answers["/api/maintenance/scope"] = _FULL

    scope = admin.scope()

    assert log == [("GET", "/api/maintenance/scope", None)]
    assert (scope.payments, scope.files, scope.budgets) == (6958, 214, 12)
    assert (scope.imports, scope.links) == (43, 317)
    assert not scope.empty


def test_пустая_база_видна_как_пустая(calls):
    _, answers = calls
    answers["/api/maintenance/scope"] = dict.fromkeys(_FULL, 0)

    assert admin.scope().empty


def test_старый_сервер_не_ломает_раздел(calls):
    """Сервер без удаления отвечает 404 — это не ошибка, а «сервер не обновлён».

    Объём базы читается вместе с учётками и журналом одной задачей. Исключение
    здесь оставило бы администратора без всего раздела на сервере, который
    просто ещё не обновили.
    """
    _, answers = calls
    answers["/api/maintenance/scope"] = transport.ServerError("Not Found", 404)

    assert not admin.scope().supported


def test_необновлённый_сервер_не_выдаётся_за_пустую_базу(calls, application):
    """«Данных нет» при полной базе читается как потеря данных.

    Именно так это и выглядело: старый сервер отвечал 404, объём приходил
    пустым, и раздел сообщал, что стирать нечего, — при базе с тысячами оплат.
    """
    from app.core.settings import AppSettings
    from app.ui.admin_page import AdminPage

    _, answers = calls
    answers["/api/maintenance/scope"] = transport.ServerError("Not Found", 404)

    page = AdminPage(AppSettings(), lambda *_: None)
    page._apply(([], [], [], [], [], admin.scope()))

    assert "не обновлена" in page.scope_label.text()
    assert "Данных в базе нет" not in page.scope_label.text()
    assert not page.wipe_button.isEnabled()


def test_остальные_отказы_сервера_не_скрываются(calls):
    """404 — про отсутствие адреса. Отказ в правах молчать не должен."""
    _, answers = calls
    answers["/api/maintenance/scope"] = transport.ServerError("Недостаточно прав", 403)

    with pytest.raises(transport.ServerError):
        admin.scope()


# --- перечисление для диалога --------------------------------------------------

def test_перечисление_согласует_числа_со_словами():
    """«1 оплата», «2 оплаты», «5 оплат» — иначе диалог читается как отписка."""
    one = admin.Scope(payments=1, files=2, budgets=5)

    assert one.summary == "1 оплата, 2 вложения, 5 бюджетов"


def test_перечисление_знает_про_одиннадцать():
    """11–14 идут с формой множественного числа, а не по последней цифре."""
    assert admin.Scope(payments=11).summary == "11 оплат"
    assert admin.Scope(payments=21).summary == "21 оплата"


def test_перечисление_пропускает_пустое():
    """Ноль бюджетов в перечислении — лишний повод усомниться, что сотрётся."""
    scope = admin.Scope(payments=6958, links=317)

    assert scope.summary == "6958 оплат, 317 привязок"


def test_пустая_база_перечисляется_словом():
    assert admin.Scope().summary == "ничего"


# --- удаление ------------------------------------------------------------------

def test_удаление_отправляет_слово_и_пароль(calls):
    """Пароль уходит на сервер: токена для этого действия недостаточно."""
    log, answers = calls
    answers["/api/maintenance/wipe"] = _FULL

    erased = admin.wipe("пароль-администратора")

    assert log == [("POST", "/api/maintenance/wipe",
                    {"confirm": "УДАЛИТЬ", "password": "пароль-администратора"})]
    assert erased.payments == 6958


def test_удаление_возвращает_стёртое(calls):
    """Сообщение после удаления строится по ответу сервера, а не по ожиданиям."""
    _, answers = calls
    answers["/api/maintenance/wipe"] = {"payments": 3, "files": 1}

    assert admin.wipe("пароль").summary == "3 оплаты, 1 вложение"


# --- диалог подтверждения ------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.widgets.database_dialogs import WipeDialog  # noqa: E402


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(application):
    return WipeDialog(admin.Scope(**_FULL))


def test_кнопка_не_нажимается_пустой(dialog):
    assert not dialog.accept_button.isEnabled()


def test_кнопка_ждёт_и_слово_и_пароль(dialog):
    dialog.confirm.setText("УДАЛИТЬ")
    assert not dialog.accept_button.isEnabled(), "пароль ещё не введён"

    dialog.password.setText("пароль")
    assert dialog.accept_button.isEnabled()


def test_чужое_слово_кнопку_не_открывает(dialog):
    """Похожего мало: набрать нужно именно то слово, которое ждёт сервер."""
    dialog.password.setText("пароль")
    for typed in ("удалит", "УДАЛИТЬ ВСЁ", "delete", "да"):
        dialog.confirm.setText(typed)
        assert not dialog.accept_button.isEnabled(), typed


def test_слово_принимается_в_любом_регистре(dialog):
    """Регистр — не проверка внимательности, а раскладка и Caps Lock."""
    dialog.password.setText("пароль")
    dialog.confirm.setText(" удалить ")

    assert dialog.accept_button.isEnabled()


def test_объём_удаления_виден_в_диалоге(dialog):
    """Соглашаться на удаление, не видя его объёма, человеку не на чем."""
    assert "6958 оплат" in dialog.scope_text.text()
    assert "214 вложений" in dialog.scope_text.text()


def test_пароль_не_показывается_на_экране(dialog):
    from PySide6.QtWidgets import QLineEdit

    assert dialog.password.echoMode() == QLineEdit.EchoMode.Password


# --- согласование с разделом оплат ---------------------------------------------

def test_после_удаления_раздел_оплат_предупреждён(application):
    """Раздел оплат читает базу один раз за запуск.

    Без предупреждения он до перезапуска показывал бы стёртые оплаты — и
    администратор решил бы, что удаление не сработало.
    """
    from app.core.settings import AppSettings
    from app.ui.admin_page import AdminPage

    warned: list[bool] = []
    page = AdminPage(AppSettings(), lambda *_: None,
                     on_wiped=lambda: warned.append(True))

    page._wiped(admin.Scope(payments=6958))

    assert warned == [True]


def test_сброс_возвращает_разделу_оплат_чтение_базы(application, monkeypatch, tmp_path):
    from app.core.payments import data, store
    from app.core.settings import AppSettings
    from app.ui import payments_page as module

    monkeypatch.setattr(store, "database_path", lambda: str(tmp_path / "payments.db"))
    monkeypatch.setattr(data, "online", lambda: False)
    page = module.PaymentsPage(AppSettings(), lambda *_: None)

    reloads: list[bool] = []
    monkeypatch.setattr(page, "reload", lambda: reloads.append(True))
    monkeypatch.setattr(page, "_connect", lambda: None)
    monkeypatch.setattr(page, "_remind_import", lambda: None)

    page._loaded = True
    page.restore()
    assert reloads == [], "загруженная страница сама себя не перечитывает"

    page.invalidate()
    page.restore()
    assert reloads == [True]
