"""Карточка «Доступ через Финляндию» в настройках.

Запуск проверяется настоящим sing-box на временном разделе реестра, как и в
`test_vpn.py`; проверки без запуска обходятся подменой менеджера.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from app.core.settings import AppSettings
from app.core.vpn import VpnManager, binary, manager, parse, subscription, sysproxy
from app.ui.settings_page import SettingsPage
from app.ui.widgets.toast import ToastKind

LINK = ("vless://11111111-2222-3333-4444-555555555555@203.0.113.7:8080"
        "?encryption=none&security=none&type=ws&path=%2Fabc")

windows_only = pytest.mark.skipif(not sysproxy.SUPPORTED, reason="системный прокси — только Windows")
needs_sing_box = pytest.mark.skipif(
    binary.bundled_path() is None, reason="sing-box не скачан: tools/fetch_singbox.py")


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def scratch_registry():
    import winreg

    path = rf"Software\RetailCoreTest\{uuid.uuid4().hex}"
    yield sysproxy.ProxyRegistry(path)
    for sub in (path, r"Software\RetailCoreTest"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            pass


def _page(application, vpn):
    toasts: list[tuple[str, ToastKind]] = []
    page = SettingsPage(AppSettings(), lambda text, kind: toasts.append((text, kind)), vpn=vpn)
    page.toasts = toasts  # для проверок
    return page


def _settle(application) -> None:
    """Дожидается фоновых задач вместе с теми, что порождают их результаты.

    Загрузка подписки заканчивается запуском туннеля — второй задачей, поэтому
    одного ожидания мало.
    """
    for _ in range(3):
        QThreadPool.globalInstance().waitForDone(15000)
        application.processEvents()


def test_без_менеджера_карточки_нет(application):
    page = _page(application, None)

    assert not hasattr(page, "vpn_link")


@windows_only
def test_ссылка_скрыта_по_умолчанию_как_токен(application, scratch_registry):
    from PySide6.QtWidgets import QLineEdit

    page = _page(application, VpnManager(scratch_registry))

    assert page.vpn_link.echoMode() == QLineEdit.EchoMode.Password
    assert page.vpn_toggle.text() == "Включить"
    assert not page.vpn_check.isEnabled()


@windows_only
def test_неверная_ссылка_не_запускает_туннель_и_объясняет_причину(application, scratch_registry):
    vpn = VpnManager(scratch_registry)
    page = _page(application, vpn)
    page.vpn_link.setText("не ссылка")

    page.vpn_toggle.click()

    assert not vpn.active
    assert page.toasts[-1][1] == ToastKind.ERROR
    assert "vless://" in page.toasts[-1][0]
    assert "vless://" in page.vpn_status.text()
    assert scratch_registry.server() == ""
    assert not page.settings.vpn_enabled


@windows_only
@needs_sing_box
def test_включение_и_выключение_из_карточки(application, scratch_registry):
    vpn = VpnManager(scratch_registry)
    page = _page(application, vpn)
    page.vpn_link.setText(LINK)
    before = scratch_registry.snapshot()

    page.vpn_toggle.click()
    _settle(application)
    try:
        assert vpn.active
        assert page.settings.vpn_enabled
        assert page.vpn_toggle.text() == "Выключить"
        assert page.vpn_check.isEnabled()
        assert not page.vpn_link.isEnabled()
        assert manager.load_link() == LINK
        assert scratch_registry.server() == vpn.proxy_address
    finally:
        if vpn.active:
            page.vpn_toggle.click()
            _settle(application)

    assert not vpn.active
    assert not page.settings.vpn_enabled
    assert page.vpn_toggle.text() == "Включить"
    assert scratch_registry.snapshot() == before


@windows_only
def test_сбой_запуска_показывается_и_кнопка_возвращается(application, scratch_registry, monkeypatch):
    monkeypatch.setattr(binary, "bundled_path", lambda: None)
    vpn = VpnManager(scratch_registry)
    page = _page(application, vpn)
    page.vpn_link.setText(LINK)

    page.vpn_toggle.click()
    _settle(application)

    assert not vpn.active
    assert page.toasts[-1][1] == ToastKind.ERROR
    assert "sing-box" in page.vpn_status.text()
    assert page.vpn_toggle.isEnabled()
    assert not page.settings.vpn_enabled


# --- подписка ---------------------------------------------------------------------------

SUB_URL = "https://sub.example.test/sub/token"
_USER = "11111111-2222-3333-4444-555555555555"


def _server(host: str, name: str):
    return parse(
        f"vless://{_USER}@{host}:8443?type=ws&security=tls&sni={host}&host={host}"
        f"&path=%2Fp&fp=chrome&alpn=http%2F1.1&encryption=none#{name}")


@pytest.fixture
def servers(monkeypatch):
    links = [_server("se.example.test", "SE-4"), _server("fi.example.test", "FI-4")]
    monkeypatch.setattr(subscription, "fetch", lambda url, timeout=15.0: links)
    return links


@windows_only
def test_обновление_списка_показывает_серверы_и_выбирает_финский(application, scratch_registry, servers):
    vpn = VpnManager(scratch_registry)
    page = _page(application, vpn)
    page.vpn_link.setText(SUB_URL)

    page.vpn_reload.click()
    _settle(application)

    assert not vpn.active
    assert not page.vpn_server.isHidden()
    assert [page.vpn_server.itemText(i) for i in range(2)] == ["SE-4", "FI-4"]
    assert page.vpn_server.currentText() == "FI-4"
    assert "2" in page.vpn_status.text()


@windows_only
def test_обновление_списка_без_ссылки_подписки_подсказывает_что_вставить(application, scratch_registry):
    page = _page(application, VpnManager(scratch_registry))
    page.vpn_link.setText("vless://что-то")

    page.vpn_reload.click()

    assert page.toasts[-1][1] == ToastKind.WARNING
    assert "https://" in page.toasts[-1][0]


@windows_only
def test_ошибка_загрузки_подписки_показывается(application, scratch_registry, monkeypatch):
    from app.core.vpn import VlessError

    def broken(url, timeout=15.0):
        raise VlessError("Подписка не загрузилась: нет связи")

    monkeypatch.setattr(subscription, "fetch", broken)
    vpn = VpnManager(scratch_registry)
    page = _page(application, vpn)
    page.vpn_link.setText(SUB_URL)

    page.vpn_toggle.click()
    _settle(application)

    assert not vpn.active
    assert page.toasts[-1][1] == ToastKind.ERROR
    assert "нет связи" in page.vpn_status.text()
    assert page.vpn_toggle.isEnabled()


@windows_only
@needs_sing_box
def test_включение_по_подписке_поднимает_выбранный_сервер(application, scratch_registry, servers):
    vpn = VpnManager(scratch_registry)
    page = _page(application, vpn)
    page.vpn_link.setText(SUB_URL)

    page.vpn_toggle.click()
    _settle(application)
    try:
        assert vpn.active
        # В файле ссылки лежит сервер, а не подписка: запуск приложения не ходит в сеть.
        assert manager.load_link() == servers[1].raw
        assert manager.load_subscription() == SUB_URL
        assert page.settings.vpn_server == "FI-4"
        assert "FI-4" in page.vpn_status.text()
    finally:
        vpn.stop()


def test_выбор_сервера_запоминается_по_названию(application, scratch_registry, servers):
    page = _page(application, VpnManager(scratch_registry))
    page.vpn_link.setText(SUB_URL)
    page.vpn_reload.click()
    _settle(application)

    page.vpn_server.setCurrentIndex(0)

    assert page.settings.vpn_server == "SE-4"
    assert subscription.preferred(servers, page.settings.vpn_server).name == "SE-4"
