"""Пути к ресурсам: одинаково работают из исходников и из собранного .exe."""
from __future__ import annotations

import sys
from pathlib import Path


def asset_dir() -> Path:
    """Каталог с иконками и SVG.

    В сборке PyInstaller файлы распаковываются во временную папку `_MEIPASS`,
    поэтому путь от `__file__` там не годится.
    """
    if bundle := getattr(sys, "_MEIPASS", None):
        return Path(bundle) / "app" / "ui" / "assets"
    return Path(__file__).parent / "assets"


def asset(name: str) -> Path:
    return asset_dir() / name


def asset_url(name: str) -> str:
    """Путь для QSS: прямые слеши, кавычки добавляет вызывающий."""
    return asset(name).as_posix()


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)
