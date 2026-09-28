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


# --- журнал изменений в настройках ---------------------------------------------------

SAMPLE = """# Что нового в RetailCore

Пункты каждого раздела попадают в окно обновления.

## 3.6.5

- Аналитика по чекам
- Закрытый отчёт затемнён: сумма < 0 и Z&R экранируются

## Черновик

- это не версия, в журнал не идёт

## v3.6.4

- Направления расходов
"""


def test_журнал_собирает_все_версии_по_порядку() -> None:
    found = changelog.releases(SAMPLE)
    assert [release.version for release in found] == ["3.6.5", "3.6.4"]
    assert found[0].lines == ("Аналитика по чекам",
                              "Закрытый отчёт затемнён: сумма < 0 и Z&R экранируются")
    assert found[1].lines == ("Направления расходов",)


def test_вшитый_журнал_знает_текущую_версию() -> None:
    """Первой она может и не стоять: описание следующей версии пишется до
    того, как ей присвоят номер."""
    assert __version__ in [release.version for release in changelog.bundled_releases()]


def test_журнал_в_настройках() -> None:
    import os

    import pytest

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from app.ui.settings_page import SettingsPage, _changelog_html

    page = SettingsPage(AppSettings(), lambda *_: None)
    text = page.changelog_view.toPlainText()
    assert f"Версия {__version__} · установлена" in text

    html = _changelog_html(changelog.releases(SAMPLE), "3.6.4")
    assert "сумма &lt; 0 и Z&amp;R" in html
    assert html.index("3.6.5") < html.index("3.6.4 <span")
