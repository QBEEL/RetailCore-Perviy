"""Где взять sing-box: он поставляется внутри приложения.

Из временной папки onefile-сборки его запускать нельзя: она пересоздаётся при
каждом старте, и антивирус проверял бы «новый» исполняемый файл каждый раз.
Поэтому при первом включении файл копируется в папку данных и живёт там.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .. import appdata

EXE_NAME = "sing-box.exe"
BUNDLE_DIR = "vpn"


class BinaryMissing(RuntimeError):
    """В сборке нет sing-box; текст ошибки показывается человеку."""


def work_dir() -> str:
    return os.path.join(appdata.data_dir(), "vpn")


def installed_path() -> str:
    return os.path.join(work_dir(), EXE_NAME)


def bundled_path() -> Path | None:
    """Файл в сборке или, при запуске из исходников, в vendor/sing-box."""
    if getattr(sys, "frozen", False):
        candidate = Path(getattr(sys, "_MEIPASS", "")) / BUNDLE_DIR / EXE_NAME
    else:
        candidate = Path(__file__).resolve().parents[3] / "vendor" / "sing-box" / EXE_NAME
    return candidate if candidate.is_file() else None


def ensure_installed(source: Path | None = None) -> str:
    """Путь к рабочей копии sing-box; при необходимости копирует её из сборки."""
    target = installed_path()
    bundled = source or bundled_path()
    if bundled is None:
        if os.path.isfile(target):
            return target
        raise BinaryMissing(
            "В этой сборке нет компонента sing-box. Для запуска из исходников выполните "
            "tools/fetch_singbox.py.")
    # Совпадение размера — признак той же версии: sing-box меняется целиком, а
    # хеш 45 МБ на каждом включении считать незачем.
    if os.path.isfile(target) and os.path.getsize(target) == bundled.stat().st_size:
        return target
    os.makedirs(work_dir(), exist_ok=True)
    temporary = f"{target}.tmp"
    try:
        shutil.copyfile(bundled, temporary)
        os.replace(temporary, target)
    except OSError as error:
        # Работающий sing-box другого окна держит файл: тогда берём тот, что есть.
        if os.path.isfile(target):
            return target
        raise BinaryMissing(f"Не удалось подготовить sing-box: {error}") from error
    return target
