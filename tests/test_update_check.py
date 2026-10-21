"""Проверка обновлений: версия берётся без API GitHub, API — запасной путь.

У API без токена лимит 60 запросов в час с одного IP: офис за общим адресом
упирался в него на утреннем запуске, и менеджеры видели «status code 403».
Здесь вместо GitHub — локальный сервер, который отвечает так же, как он:
перенаправлением с `releases/latest` на тег релиза.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtNetwork import QNetworkProxy  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import __version__  # noqa: E402
from app.core import updater  # noqa: E402
from app.ui import update_check  # noqa: E402
from app.ui.update_check import UpdateChecker  # noqa: E402

NEWER = "99.0.0"
MANIFEST = {"changelog": ["Новое"], "sha256": "ab" * 32, "mandatory": False}


class FakeGithub:
    """Что отвечает «GitHub»: сценарий задаётся полями, обращения запоминаются."""

    def __init__(self) -> None:
        self.latest = ("redirect", f"/releases/tag/v{NEWER}")
        self.api = 403
        self.manifest_status = 200
        self.requests: list[str] = []
        self.base = ""

    def handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append(self.path)
                if self.path == "/releases/latest":
                    kind, target = outer.latest
                    if kind == "redirect":
                        self.send_response(302)
                        self.send_header("Location", outer.base + target)
                        self.end_headers()
                    else:
                        self._send(200, b"<html>no redirect</html>")
                elif self.path == f"/releases/download/v{NEWER}/version.json":
                    self._send(outer.manifest_status, json.dumps(MANIFEST).encode())
                elif self.path == "/api/latest":
                    if outer.api == 200:
                        asset = updater.release_asset()
                        body = {"tag_name": f"v{NEWER}", "assets": [
                            {"name": asset, "browser_download_url":
                                f"{outer.base}/releases/download/v{NEWER}/{asset}"},
                            {"name": "version.json", "browser_download_url":
                                f"{outer.base}/releases/download/v{NEWER}/version.json"}]}
                        self._send(200, json.dumps(body).encode())
                    else:
                        self._send(outer.api, b'{"message": "API rate limit exceeded"}')
                else:
                    self._send(404, b"")

            def _send(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        return Handler


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch):
    fake = FakeGithub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    fake.base = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(update_check, "_RELEASES_URL", f"{fake.base}/releases")
    monkeypatch.setattr(update_check, "_LATEST_URL", f"{fake.base}/releases/latest")
    monkeypatch.setattr(update_check, "_API_URL", f"{fake.base}/api/latest")
    # Сервер локальный и без TLS: проверка «только HTTPS» в этих тестах мешает.
    monkeypatch.setattr(updater, "is_https", lambda url: True)
    monkeypatch.setattr(updater, "log_event", lambda message: None)
    yield fake
    server.shutdown()
    server.server_close()


# Проверяльщики живут до конца прогона: сборка мусора, уничтожающая QObject с
# сетевым менеджером посреди отложенных удалений ответов, роняет Qt.
_keep: list[UpdateChecker] = []


def _run(application, checker: UpdateChecker) -> dict[str, object]:
    _keep.append(checker)
    outcome: dict[str, object] = {}
    checker.found.connect(lambda manifest: outcome.update(found=manifest))
    checker.up_to_date.connect(lambda: outcome.update(up_to_date=True))
    checker.error.connect(lambda message: outcome.update(error=message))
    checker._net.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))

    checker.check()

    end = time.monotonic() + 8
    while not outcome and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)
    assert outcome, "проверка не завершилась"
    for _ in range(20):
        QApplication.processEvents()
        time.sleep(0.005)
    return outcome


def test_version_is_found_without_touching_the_api(application, github) -> None:
    outcome = _run(application, UpdateChecker())

    manifest = outcome["found"]
    assert manifest.version == NEWER
    assert manifest.changelog == ["Новое"]
    assert manifest.sha256 == updater.manifest_hash(MANIFEST)
    assert manifest.exe_url == (
        f"{github.base}/releases/download/v{NEWER}/{updater.release_asset()}")
    assert "/api/latest" not in github.requests, "лимит API не расходуется"


def test_rate_limited_api_does_not_matter_when_redirect_works(application, github) -> None:
    github.api = 403

    outcome = _run(application, UpdateChecker())

    assert "found" in outcome and "error" not in outcome


def test_falls_back_to_api_when_redirect_is_missing(application, github) -> None:
    github.latest = ("page", "")
    github.api = 200

    outcome = _run(application, UpdateChecker())

    assert outcome["found"].version == NEWER
    assert "/api/latest" in github.requests


def test_reports_error_when_both_ways_fail(application, github) -> None:
    github.latest = ("page", "")
    github.api = 403

    outcome = _run(application, UpdateChecker())

    assert "Не удалось проверить обновления" in outcome["error"]
    assert "/api/latest" in outcome["error"], "причина — отказ запасного пути, а не первого"


def test_current_version_is_up_to_date(application, github, monkeypatch) -> None:
    monkeypatch.setattr(update_check, "__version__", NEWER)

    outcome = _run(application, UpdateChecker())

    assert outcome == {"up_to_date": True}


@pytest.mark.parametrize("target", [
    "/releases/tag/../../evil",
    "/releases/tag/v1.2",
    "/releases/tag/v1.2.3-beta",
    "/releases/tag/latest",
    "/releases",
])
def test_odd_redirect_target_is_not_trusted(application, github, target: str) -> None:
    """Тег идёт в адрес загрузки: всё, кроме номера версии, отклоняется."""
    github.latest = ("redirect", target)
    github.api = 200

    outcome = _run(application, UpdateChecker())

    assert "/api/latest" in github.requests, "непонятный ответ — запасной путь"
    assert outcome["found"].version == NEWER


def test_missing_manifest_is_reported(application, github) -> None:
    github.manifest_status = 404

    outcome = _run(application, UpdateChecker())

    assert "описание релиза" in outcome["error"]


def test_this_build_is_older_than_the_fake_release() -> None:
    assert updater.is_newer(NEWER, __version__)
