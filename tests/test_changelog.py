"""Список изменений: вшитый CHANGELOG и отметка увиденной версии."""
from __future__ import annotations

import json
from pathlib import Path

from app import __version__
from app.core import changelog
from app.core.settings import AppSettings


def test_bundled_changelog_describes_current_version() -> None:
    """Без раздела текущей версии окно «Что нового» после обновления пустое."""
    assert changelog.bundled(__version__)


def test_upgrade_is_shown_once_and_not_on_first_install() -> None:
    assert changelog.is_upgrade("3.4.0", "3.4.1")
    assert not changelog.is_upgrade("3.4.1", "3.4.1")
    assert not changelog.is_upgrade("", "3.4.1")


def test_settings_from_older_version_count_as_upgrade(tmp_path: Path) -> None:
    """Версии до 3.4.2 отметку не вели — их настройки значат обновление."""
    old = tmp_path / "settings.json"
    old.write_text(json.dumps({"update_check_auto": True}), encoding="utf-8")
    assert changelog.is_upgrade(AppSettings.load(str(old)).seen_version, __version__)

    fresh = AppSettings.load(str(tmp_path / "нет.json"))
    assert fresh.seen_version == ""


def test_seen_version_survives_save(tmp_path: Path) -> None:
    path = str(tmp_path / "settings.json")
    settings = AppSettings.load(path)
    settings.seen_version = "3.4.2"
    settings.save()
    assert AppSettings.load(path).seen_version == "3.4.2"
