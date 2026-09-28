"""Права на разделы: чего администратор закрыл, того человек не видит.

Маркировкой в отделе занимаются двое, а вкладка висела у всех восемнадцати.
Здесь проверяется то, ради чего всё затевалось: закрытый раздел пропадает из
меню и не открывается сочетанием клавиш, закрытая стартовая страница уступает
первой открытой, а неделя работы без сервера не возвращает человеку вкладки,
которые ему закрыли.

Проверяется и обратное — что закрыть можно не всё: «Настройки» и личный
кабинет остаются, иначе человек не починит даже собственное подключение.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.core import appdata
from app.core.payments import session_store, transport
from app.core.settings import AppSettings
from app.ui import pages
from app.ui.pages import MANAGED, PAGES, index_of


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def profile(monkeypatch, tmp_path):
    """Профиль приложения в отдельной папке — рядом с настоящим не ходим."""
    monkeypatch.setattr(appdata, "data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(appdata, "path_to", lambda name: str(tmp_path / name))
    return tmp_path


@pytest.fixture
def closed(request):
    """Сессия, которой закрыли перечисленные разделы. После теста — чистая."""
    transport.session.clear()
    transport.session.denied_pages = tuple(request.param)
    yield transport.session
    transport.session.clear()


def _window(application) -> object:
    from app.ui.main_window import MainWindow

    return MainWindow(AppSettings(), offline=True)


# --- меню ---------------------------------------------------------------------------

@pytest.mark.parametrize("closed", [("marking",)], indirect=True)
def test_закрытый_раздел_пропадает_из_меню(application, closed):
    window = _window(application)

    assert window._nav_group.button(index_of("marking")).isHidden()
    # Соседние разделы трогать нельзя: закрыт один, а не всё подряд.
    assert not window._nav_group.button(index_of("catalog")).isHidden()


@pytest.mark.parametrize("closed", [("marking",)], indirect=True)
def test_сочетание_клавиш_не_открывает_закрытый_раздел(application, closed):
    """Ctrl+M остаётся нажатым, но приводить теперь никуда не должен."""
    window = _window(application)
    was = window.pages.currentIndex()

    assert window.show_page(index_of("marking")) is False
    assert window.pages.currentIndex() == was


@pytest.mark.parametrize("closed", [("match", "order", "price")], indirect=True)
def test_закрытая_стартовая_страница_уступает_первой_открытой(application, closed):
    """Иначе запуск встречал бы человека пустым окном без единой кнопки."""
    window = _window(application)

    assert window.pages.currentIndex() == index_of("payments")


@pytest.mark.parametrize("closed", [()], indirect=True)
def test_без_ограничений_видно_всё(application, closed):
    window = _window(application)

    hidden = [page.title for index, page in enumerate(PAGES)
              if page.managed and window._nav_group.button(index).isHidden()]
    assert hidden == []


# --- что закрыть нельзя -------------------------------------------------------------

def test_настройки_и_администрирование_закрыть_нельзя():
    """Настройки — единственный путь починить подключение, а раздел
    администратора управляется своим признаком: второй механизм для того же
    рано или поздно разошёлся бы с первым."""
    codes = {page.code for page in MANAGED}

    assert "settings" not in codes and "admin" not in codes


@pytest.mark.parametrize("closed", [("settings",)], indirect=True)
def test_настройки_остаются_даже_если_их_закрыли(application, closed):
    """Права приходят с сервера строками — устаревший код не должен сработать."""
    window = _window(application)

    assert not window._nav_group.button(index_of("settings")).isHidden()


# --- сессия и льготный срок ---------------------------------------------------------

def test_права_переживают_перезапуск(profile):
    """Иначе неделя без сервера возвращала бы закрытые вкладки."""
    session_store.save(transport.Session(
        base_url="https://retail.test", token="токен", login="e.ivanov",
        denied_pages=("marking",),
        expires_at=datetime.now() + timedelta(hours=12)))

    transport.restore(session_store.load())

    assert transport.session.denied_pages == ("marking",)
    assert not transport.session.may_open("marking")
    assert transport.session.may_open("payments")
    transport.session.clear()


def test_сервер_прежней_версии_оставляет_все_разделы():
    """Обновлять приложение и сервер в один час не выйдет — старый ответ о
    правах не знает, и это должно значить «открыто всё», а не «закрыто всё»."""
    transport.session.clear()

    transport._adopt({"access_token": "токен", "login": "e.ivanov",
                      "full_name": "Иванов Евгений", "is_admin": False,
                      "expires_in": 3600})

    assert transport.session.denied_pages == ()
    assert transport.session.may_open("marking")
    transport.session.clear()


# --- карточка учётной записи --------------------------------------------------------

def test_карточка_учётки_собирает_снятые_разделы(application):
    from app.core.payments.admin import Account
    from app.ui.widgets.account_dialogs import AccountDialog

    dialog = AccountDialog(Account(login="e.ivanov", full_name="Иванов Евгений"),
                           known_responsible=[])
    for code, box in dialog.pages:
        if code == "marking":
            box.setChecked(False)

    assert dialog.result_account().denied_pages == ["marking"]


def test_карточка_учётки_показывает_уже_закрытое(application):
    """Открыв чужую учётку, администратор обязан видеть её нынешние права."""
    from app.core.payments.admin import Account
    from app.ui.widgets.account_dialogs import AccountDialog

    account = Account(login="e.ivanov", full_name="Иванов Евгений",
                      denied_pages=["marking"])
    dialog = AccountDialog(account, known_responsible=[])

    checked = {code: box.isChecked() for code, box in dialog.pages}
    assert checked["marking"] is False and checked["payments"] is True


# --- таблица учётных записей --------------------------------------------------------

def test_в_таблице_видно_что_закрыто():
    assert pages.describe(["marking"]) == "закрыто: Маркировка"
    # Обычная учётка не должна нести в таблице ни слова: строк два десятка, и
    # «доступны все разделы» в каждой — это шум ради исключений.
    assert pages.describe([]) == ""


# --- отдельные отчёты ---------------------------------------------------------------

def _reports(window) -> dict[str, bool]:
    """Отчёт → открыт ли он. Закрытый остаётся вкладкой, но под затемнением."""
    tabs = window.reports_page.tabs
    return {tabs.tabText(i): not window.reports_page.locks[i].locked
            for i in range(tabs.count())}


@pytest.mark.parametrize("closed", [("reports.receipts",)], indirect=True)
def test_закрытый_отчёт_затемнён_а_не_спрятан(application, closed):
    """Отчёт, о котором рассказали коллеги, человек должен найти и понять, почему нельзя."""
    window = _window(application)
    page = window.reports_page

    assert _reports(window) == {"Отчёт поставщику": True,
                                "Ведомость по складам": True,
                                "Аналитика по чекам": False}
    lock = page.locks[2]
    assert page.tabs.isTabVisible(2)
    assert not lock.content.isEnabled(), "кнопки под затемнением недоступны"
    assert "обратитесь к администратору" in lock.overlay.message.text()
    assert lock.overlay.movie.isValid(), "анимация должна загрузиться из сборки"
    assert not window._nav_group.button(index_of("reports")).isHidden()


@pytest.mark.parametrize(
    "closed", [("reports.supplier", "reports.ledger", "reports.receipts")], indirect=True)
def test_раздел_с_закрытыми_отчётами_остаётся_в_меню(application, closed):
    """Закрыть «Отчётность» из меню — это снять весь раздел, а не все три отчёта."""
    window = _window(application)

    assert not window._nav_group.button(index_of("reports")).isHidden()
    assert not any(_reports(window).values())


@pytest.mark.parametrize("closed", [("reports.supplier",)], indirect=True)
def test_открывается_первый_доступный_отчёт(application, closed):
    window = _window(application)

    assert window.reports_page.tabs.currentIndex() == 1


def test_смена_входа_снимает_затемнение(application):
    """Страница строится до входа: права применяются заново при каждой смене."""
    transport.session.clear()
    transport.session.denied_pages = ("reports.receipts",)
    try:
        window = _window(application)
        assert _reports(window)["Аналитика по чекам"] is False
        transport.session.denied_pages = ()
        window._sync_page_access()
        assert _reports(window)["Аналитика по чекам"] is True
        assert window.reports_page.locks[2].content.isEnabled()
    finally:
        transport.session.clear()


def test_карточка_учётки_закрывает_отдельный_отчёт(application):
    from app.core.payments.admin import Account
    from app.ui.widgets.account_dialogs import AccountDialog

    dialog = AccountDialog(Account(login="k.valeva", full_name="Валева Карина"),
                           known_responsible=[])
    boxes = dict(dialog.pages)
    assert {"reports.supplier", "reports.ledger", "reports.receipts"} <= set(boxes)

    boxes["reports.receipts"].setChecked(False)
    assert dialog.result_account().denied_pages == ["reports.receipts"]


def test_флажки_отчётов_гаснут_с_разделом(application):
    """Закрытый раздел закрывает и отчёты, но их отметки сохраняются."""
    from app.core.payments.admin import Account
    from app.ui.widgets.account_dialogs import AccountDialog

    account = Account(login="k.valeva", full_name="Валева Карина",
                      denied_pages=["reports.receipts"])
    dialog = AccountDialog(account, known_responsible=[])
    boxes = dict(dialog.pages)

    boxes["reports"].setChecked(False)
    assert not boxes["reports.ledger"].isEnabled()
    boxes["reports"].setChecked(True)
    assert boxes["reports.ledger"].isEnabled()
    assert boxes["reports.receipts"].isChecked() is False


def test_в_таблице_видно_закрытый_отчёт():
    assert pages.describe(["reports.receipts"]) == "закрыто: Аналитика по чекам"
    # Закрыт весь раздел — его отчёты не перечисляются второй раз.
    assert pages.describe(["reports", "reports.receipts"]) == "закрыто: Отчётность"


def test_анимация_обновления_только_на_время_загрузки(application, monkeypatch):
    from app.ui.update_check import UpdateChecker
    from app.ui.widgets import update_dialog as module
    from app.core import updater

    dialog = module.UpdateDialog(AppSettings(), UpdateChecker())
    assert dialog._movie.isValid(), "анимация должна загрузиться из сборки"
    assert dialog._animation.isHidden()

    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(dialog.checker, "download", lambda manifest: None)
    manifest = updater.ReleaseManifest(version="9.9.9", exe_url="")
    dialog._manifest = manifest
    dialog._start_update(manifest)
    assert not dialog._animation.isHidden()

    dialog._on_error("сеть пропала")
    assert dialog._animation.isHidden(), "после ошибки анимация не нужна"


def test_анимация_и_при_автозагрузке(application, monkeypatch):
    """Обновление, которое качается само, — тоже загрузка, и тоже с анимацией."""
    from app.ui.update_check import UpdateChecker
    from app.ui.widgets import update_dialog as module
    from app.core import updater

    settings = AppSettings()
    settings.update_download_auto = True
    dialog = module.UpdateDialog(settings, UpdateChecker())
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(dialog.checker, "download", lambda manifest: None)
    monkeypatch.setattr(dialog, "_is_snoozed", lambda: False)
    dialog._on_found(updater.ReleaseManifest(version="9.9.9", exe_url=""))
    assert not dialog._animation.isHidden()
