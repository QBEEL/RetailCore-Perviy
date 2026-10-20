"""Версия приложения: клиент её присылает, администратор видит, кто обновился."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import __version__  # noqa: E402
from app.core.payments import admin, transport  # noqa: E402
from app.core.payments.admin import Account  # noqa: E402
from app.ui.admin_page import _version_text  # noqa: E402


class TestHeaders:
    def test_every_request_names_the_version(self, monkeypatch) -> None:
        seen = {}

        def fake_send(request):
            seen.update(dict(request.header_items()))
            return {}

        monkeypatch.setattr(transport, "_send", fake_send)
        monkeypatch.setattr(transport.session, "token", "t")
        monkeypatch.setattr(transport.session, "base_url", "https://example.test")
        transport.get("/api/health")
        assert seen["X-app-version"] == __version__
        assert seen["X-app-platform"] in ("windows", "macos", "linux")
        assert seen["Authorization"] == "Bearer t"

    def test_version_looks_like_the_server_expects(self) -> None:
        # Сервер принимает только вид «3.9.2» и остальное отбрасывает.
        import re

        assert re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$", transport.CLIENT_HEADERS["X-App-Version"])

    @pytest.mark.parametrize("platform, expected", [
        ("win32", "windows"), ("darwin", "macos"), ("linux", "linux")])
    def test_platform_names(self, monkeypatch, platform, expected) -> None:
        monkeypatch.setattr(transport.sys, "platform", platform)
        assert transport._platform() == expected


def person(name="КМ", version="", active=True, **extra) -> Account:
    return Account(id=1, login=name, full_name=name, app_version=version,
                   is_active=active, **extra)


class TestSummary:
    def test_counts_those_on_the_current_version(self) -> None:
        people = [person("Иванов", "3.9.2"), person("Петров", "3.10.0"),
                  person("Сидоров", "3.9.1"), person("Кузнецов", "")]
        text, behind = admin.update_summary(people, "3.9.2")
        assert [a.full_name for a in behind] == ["Сидоров", "Кузнецов"]
        assert text.startswith("Версия 3.9.2 или новее: 2 из 4")
        assert "Сидоров" in text and "Кузнецов" in text

    def test_versions_compare_as_numbers_not_text(self) -> None:
        # Как строки «3.10.0» меньше «3.9.2» — для проверки это была бы ошибка.
        assert admin.version_key("3.10.0") > admin.version_key("3.9.2")
        _, behind = admin.update_summary([person("А", "3.10.0")], "3.9.2")
        assert behind == []

    def test_disabled_accounts_do_not_count(self) -> None:
        # Уволенный сотрудник, не вошедший с обновления, не должен держать закрытие.
        people = [person("Иванов", "3.9.2"), person("Бывший", "", active=False)]
        text, behind = admin.update_summary(people, "3.9.2")
        assert behind == [] and text == "Версия 3.9.2 или новее: 1 из 1"

    def test_long_lists_are_cut(self) -> None:
        people = [person(f"Человек {n}") for n in range(9)]
        text, _ = admin.update_summary(people, "3.9.2")
        assert "и ещё 3" in text

    def test_garbage_version_counts_as_behind(self) -> None:
        _, behind = admin.update_summary([person("А", "abc")], "3.9.2")
        assert len(behind) == 1


class TestAccount:
    def test_read_from_server_answer(self) -> None:
        when = (datetime.now() - timedelta(minutes=5)).isoformat()
        account = admin._account({
            "id": 5, "login": "km", "full_name": "КМ", "responsible": [], "is_admin": False,
            "is_active": True, "created_at": None, "app_version": "3.9.2",
            "app_platform": "windows", "app_seen_at": when})
        assert account.app_version == "3.9.2" and account.app_seen_at is not None

    def test_old_server_without_the_fields(self) -> None:
        account = admin._account({"id": 6, "login": "x", "full_name": "", "responsible": [],
                                  "is_admin": False, "is_active": True})
        assert account.app_version == "" and account.app_seen_at is None

    def test_column_text(self) -> None:
        assert _version_text(person(version="3.9.2", app_platform="windows")) == "3.9.2 · Windows"
        assert _version_text(person(version="3.9.2", app_platform="macos")) == "3.9.2 · macOS"
        assert _version_text(person(version="3.9.2")) == "3.9.2"
        assert _version_text(person(version="")) == "—"
