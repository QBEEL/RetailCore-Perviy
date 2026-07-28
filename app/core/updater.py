"""Логика автообновления: версии, целостность файла, подмена exe, журнал.

Модуль не зависит от Qt и сети — запросы к GitHub делает `app/ui/update_check.py`,
здесь только то, что можно проверить в pytest без запущенного event loop.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

from . import appdata

LOG_FILE = "update.log"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass
class ReleaseManifest:
    """Сведения о найденном на GitHub релизе."""

    version: str
    exe_url: str
    mandatory: bool = False
    sha256: str = ""
    changelog: list[str] = field(default_factory=list)


def parse_version(text: str) -> tuple[int, int, int]:
    """"v1.2.0" / "1.2.0" → (1, 2, 0). Нераспознанное значение — (0, 0, 0)."""
    match = _VERSION_RE.search(text or "")
    if not match:
        return (0, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_newer(remote: str, local: str) -> bool:
    return parse_version(remote) > parse_version(local)


def is_https(url: str) -> bool:
    return url.lower().startswith("https://")


def verify_sha256(path: str, expected: str) -> bool:
    """Пустой `expected` означает «хеш не публиковался» — проверка пропускается."""
    if not expected:
        return True
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower() == expected.strip().lower()


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def current_exe_path() -> str:
    return sys.executable


def apply_update(new_exe_path: str, current_path: str | None = None) -> None:
    """Подменяет запущенный exe и перезапускает приложение под тем же именем.

    Windows позволяет переименовать exe-файл работающего процесса (в отличие
    от удаления), поэтому отдельный updater-процесс не нужен: переименовываем
    старый файл, ставим новый на его место и запускаем — процесс, вызвавший
    эту функцию, должен завершиться сразу после (см. `update_check.py`).
    """
    current = current_path or current_exe_path()
    old_path = _old_exe_path(current)
    if os.path.exists(old_path):
        os.remove(old_path)
    os.rename(current, old_path)
    os.replace(new_exe_path, current)
    subprocess.Popen([current], env=child_environment(), close_fds=True,
                      creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def child_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Окружение для новой версии — без служебных переменных PyInstaller.

    Onefile-сборка держит распакованные файлы во временной папке и сообщает
    её путь через `_PYI_APPLICATION_HOME_DIR`. Унаследовав эту переменную,
    новый процесс не распаковывался бы заново, а сел бы в папку старого — а
    тот, завершаясь, её удаляет. Приложение запускалось и падало позже, на
    первом обращении к файлу ресурсов.
    """
    environment = dict(os.environ if source is None else source)
    for name in [n for n in environment if n.startswith(("_PYI", "_MEIPASS"))]:
        del environment[name]
    return environment


def cleanup_leftover(current_path: str | None = None) -> None:
    """Удаляет `*.old.exe`, оставшийся от предыдущего самообновления."""
    current = current_path or current_exe_path()
    old_path = _old_exe_path(current)
    if not os.path.exists(old_path):
        return
    try:
        os.remove(old_path)
    except OSError:
        pass  # предыдущий процесс мог ещё не завершиться — удалим в другой раз


def _old_exe_path(current: str) -> str:
    root, ext = os.path.splitext(current)
    return f"{root}.old{ext}"


def log_path() -> str:
    return appdata.path_to(LOG_FILE)


def log_event(message: str) -> None:
    appdata.log_event(LOG_FILE, message)
