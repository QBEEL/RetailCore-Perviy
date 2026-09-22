"""Список изменений версии из CHANGELOG.md.

Один разбор на двоих: сборка кладёт пункты в `version.json` для окна
обновления, а само приложение читает вшитый в сборку CHANGELOG и при первом
запуске новой версии показывает, что в ней поменялось.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .updater import parse_version

_FILE = "CHANGELOG.md"


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
