"""Список изменений версии из CHANGELOG.md.

Один разбор на троих: сборка кладёт пункты в `version.json` для окна
обновления, приложение при первом запуске новой версии показывает, что в ней
поменялось, а журнал в настройках — все версии подряд.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .updater import parse_version

_FILE = "CHANGELOG.md"
_VERSION = re.compile(r"\d+\.\d+\.\d+")


def section(text: str, version: str) -> list[str]:
    """Пункты раздела «## <версия>»; нет раздела — пустой список."""
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            # Раздел версии начинается здесь и кончается следующим таким же.
            if inside:
                break
            inside = line[3:].strip().lstrip("vV") == version
            continue
        if inside and (stripped := line.strip().lstrip("-*").strip()):
            lines.append(stripped)
    return lines


def read(path: Path, version: str) -> list[str]:
    if not path.exists():
        return []
    return section(path.read_text("utf-8"), version)


@dataclass(frozen=True, slots=True)
class Release:
    """Раздел CHANGELOG: версия и её пункты."""

    version: str
    lines: tuple[str, ...]


def releases(text: str) -> list[Release]:
    """Все версии файла в том порядке, в каком они записаны — от новой к старой.

    Разделом версии считается только заголовок с номером: вводный текст над
    первой версией и возможные служебные разделы в журнал не попадают.
    """
    found: list[Release] = []
    version = ""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if version:
                found.append(Release(version, tuple(lines)))
            title = line[3:].strip().lstrip("vV")
            version = title if _VERSION.fullmatch(title) else ""
            lines = []
            continue
        if version and (stripped := line.strip().lstrip("-*").strip()):
            lines.append(stripped)
    if version:
        found.append(Release(version, tuple(lines)))
    return found


def bundled_releases() -> list[Release]:
    path = bundled_path()
    return releases(path.read_text("utf-8")) if path.exists() else []


def bundled_path() -> Path:
    """CHANGELOG рядом с кодом: в сборке PyInstaller — в папке `_MEIPASS`."""
    if bundle := getattr(sys, "_MEIPASS", None):
        return Path(bundle) / _FILE
    return Path(__file__).resolve().parents[2] / _FILE


def bundled(version: str) -> list[str]:
    return read(bundled_path(), version)


def is_upgrade(seen: str, current: str) -> bool:
    """Запуск после обновления. Пустая отметка — первая установка: рассказывать
    о переменах тому, кто программу ещё не видел, незачем."""
    return bool(seen) and parse_version(current) > parse_version(seen)
