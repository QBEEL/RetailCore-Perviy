"""Папка пользовательских данных приложения и запись журналов.

Настройки, база снимков и логи лежат рядом в одной папке профиля, поэтому
путь к ней строится в одном месте, а не повторяется в каждом модуле.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime

APP_NAME = "RetailCore"

# Папка версий до переименования проекта: её содержимое переносится один раз.
LEGACY_APP_NAME = "ExcelDataMatcher"


def _profile() -> str:
    return os.environ.get("APPDATA") or os.path.expanduser("~")


def data_dir() -> str:
    return os.path.join(_profile(), APP_NAME)


def legacy_data_dir() -> str:
    return os.path.join(_profile(), LEGACY_APP_NAME)


def path_to(file_name: str) -> str:
    return os.path.join(data_dir(), file_name)


def adopt_legacy_data() -> bool:
    """Переносит настройки, историю и журналы из папки прежнего имени.

    Пользователь не должен потерять свои настройки и снимки из-за того, что
    приложение переименовали. Перенос делается один раз: если новая папка уже
    существует, старая не трогается.
    """
    source, destination = legacy_data_dir(), data_dir()
    if os.path.exists(destination) or not os.path.isdir(source):
        return False
    try:
        shutil.move(source, destination)
        return True
    except OSError:
        return False


def log_event(file_name: str, message: str) -> None:
    """Дописывает запись в журнал. Ошибка записи не должна ронять приложение."""
    path = path_to(file_name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M}\n{message}\n\n")
    except OSError:
        pass
