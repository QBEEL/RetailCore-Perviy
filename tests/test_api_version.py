"""Тесты получения и отображения версии API после версии приложения."""
from __future__ import annotations

import io
import json
import urllib.error
from typing import Any, Callable

import pytest
from PySide6.QtWidgets import QApplication

from app import __version__
from app.core.payments import transport
from app.core.settings import AppSettings
from app.ui import main_window as main_window_mod
from app.ui.main_window import MainWindow
from server.api.app.version import API_VERSION


class _FakeHTTPResponse:
    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.status = status
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_server_version_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_SECRET", "test-only-" + "x" * 40)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")
    from server.api.app.main import app as server_app

    assert API_VERSION == "3.15.1"
    assert server_app.version == API_VERSION
    route = next(route for route in server_app.routes if getattr(route, "path", None) == "/api/version")
    assert route.endpoint() == {"version": API_VERSION}


def test_fetch_api_version_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout=None, context=None):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        payload = json.dumps({"version": "3.15.0"}).encode("utf-8")
        return _FakeHTTPResponse(payload, status=200)

    monkeypatch.setattr(transport.urllib.request, "urlopen", fake_urlopen)
    assert transport.fetch_api_version("https://payments.example.test/") == "3.15.0"
    assert captured["url"] == "https://payments.example.test/api/version"
    assert captured["timeout"] == 5


def test_fetch_api_version_401_preserves_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport.session, "base_url", "https://payments.example.test")
    monkeypatch.setattr(transport.session, "token", "secret-token-123")
    monkeypatch.setattr(transport.session, "login", "manager")

    def fake_urlopen(request, timeout=None, context=None):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"detail":"unauthorized"}'),
        )

    monkeypatch.setattr(transport.urllib.request, "urlopen", fake_urlopen)
    assert transport.fetch_api_version("https://payments.example.test") == ""
    assert transport.session.token == "secret-token-123"
    assert transport.session.login == "manager"


@pytest.mark.parametrize(
    "raw_body",
    [
        b"not-json",
        b'{"version": ""}',
        b'{"version": "3.15"}',
        b'{"version": "v3.15.0"}',
        b'{"version": "<b>3.15.0</b>"}',
        b'{"version": 315}',
        b'[]',
    ],
)
def test_fetch_api_version_rejects_invalid_payloads(
    monkeypatch: pytest.MonkeyPatch,
    raw_body: bytes,
) -> None:
    monkeypatch.setattr(
        transport.urllib.request,
        "urlopen",
        lambda request, timeout=None, context=None: _FakeHTTPResponse(raw_body, status=200),
    )
    assert transport.fetch_api_version("https://payments.example.test") == ""


def test_main_window_version_label_offline_and_async_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qapp = QApplication.instance() or QApplication([])
    scheduled: list[tuple[Callable[[], Any], Callable[[Any], None], Callable[[str], None] | None]] = []

    def fake_run_task(fn, *, on_result, on_error=None):
        scheduled.append((fn, on_result, on_error))

    monkeypatch.setattr(main_window_mod, "run_task", fake_run_task)
    monkeypatch.setattr(transport.session, "base_url", "https://server-a.example.test")
    monkeypatch.setattr(transport.session, "token", "token-a")

    settings = AppSettings(
        update_check_auto=False,
        payment_server="https://server-a.example.test",
    )
    window = MainWindow(settings, offline=True)
    assert qapp is not None

    assert window._version_label is not None
    assert window._version_label.objectName() == "BrandSub"
    assert window._version_label.text() == f"версия {__version__} · API —"
    assert scheduled == []

    # Включаем онлайн-режим и проверяем запрос версии API
    window.offline = False
    window._sync_profile_button()
    assert len(scheduled) == 1
    assert window._version_label.text() == f"версия {__version__} · API —"

    # Повторный вызов с тем же источником не дублирует сетевой запрос, пока предыдущий в полёте
    window._sync_profile_button()
    assert len(scheduled) == 1

    # Приходит успешный ответ
    _fn1, on_done1, _on_error1 = scheduled[0]
    on_done1("3.15.0")
    assert window._version_label.text() == f"версия {__version__} · API 3.15.0"

    # Сворачивание/разворачивание меню не вызывает повторный запрос к неизменившемуся источнику
    window._sync_profile_button()
    assert len(scheduled) == 1
    assert window._version_label.text() == f"версия {__version__} · API 3.15.0"

    # При смене сервера старая версия сразу сбрасывается в '?', а устаревший колбэк игнорируется
    monkeypatch.setattr(transport.session, "base_url", "https://server-b.example.test")
    window._sync_profile_button()
    assert len(scheduled) == 2
    assert window._version_label.text() == f"версия {__version__} · API —"

    # Устаревший колбэк от первого запроса не должен перезаписать метку
    on_done1("3.14.0")
    assert window._version_label.text() == f"версия {__version__} · API —"

    # Второй запрос завершился пустой строкой (сервер не поддерживает /api/version)
    _fn2, on_done2, _on_error2 = scheduled[1]
    on_done2("")
    assert window._version_label.text() == f"версия {__version__} · API —"
    window.close()
