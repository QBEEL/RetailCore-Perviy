"""Логика автообновления: версии, целостность файла, подмена exe, журнал.

Модуль не зависит от Qt и сети — запросы к GitHub делает `app/ui/update_check.py`,
здесь только то, что можно проверить в pytest без запущенного event loop.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import appdata

LOG_FILE = "update.log"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# Файл обновления в релизе — свой на каждую платформу. Имя было одно, и macOS
# скачивал сборку для Windows: контрольная сумма сходилась (она считалась по
# ней же), установка молча срывалась на запуске чужого бинарника, а подмена к
# тому моменту уже происходила — приложение оставалось сломанным.
RELEASE_ASSETS = {"darwin": "RetailCore.dmg"}
DEFAULT_ASSET = "RetailCore.exe"


class ManualInstallRequired(Exception):
    """Поставить обновление само приложение не может — нужен человек.

    Текст исключения показывается ему целиком, поэтому он должен объяснять, что
    делать, а не что сломалось.
    """


def is_macos() -> bool:
    return sys.platform == "darwin"


def release_asset() -> str:
    return RELEASE_ASSETS.get(sys.platform, DEFAULT_ASSET)


def manifest_hash(data: dict) -> str:
    """Контрольная сумма файла для этой платформы.

    Ключ `sha256` остался за сборкой для Windows: его читают уже выпущенные
    версии, и переселить их на новый ключ нельзя — они перестали бы
    обновляться вовсе. Для macOS рядом лежит свой.
    """
    return str(data.get("sha256_macos" if is_macos() else "sha256", ""))


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


def apply_update(downloaded_path: str, current_path: str | None = None) -> None:
    """Ставит скачанное обновление и запускает новую версию.

    Вызвавший процесс обязан завершиться сразу после (см. `update_dialog.py`):
    обе платформы рассчитывают, что старая копия вот-вот уйдёт.
    """
    if is_macos():
        _install_macos(downloaded_path, current_path)
    else:
        _install_windows(downloaded_path, current_path)


def app_bundle(executable: str) -> str:
    """Путь к .app по пути бинарника внутри него.

    Разбор идёт по частям пути, а не поиском «.app/Contents/MacOS» в строке:
    так функция не зависит от разделителя и её видно из тестов на любой
    системе — проверять установку для macOS больше негде.

    Пусто — значит запуск не из бандла: так бывает из исходников или из
    распакованной папки, и подменять там нечего.
    """
    for parent in Path(executable).parents:
        if parent.name.endswith(".app"):
            return str(parent)
    return ""


def _install_macos(dmg_path: str, current_path: str | None = None) -> None:
    """Заменяет .app содержимым образа и запускает новую копию.

    Бандл — папка, и подменить его как файл нельзя. Новая копия снимается с
    образа целиком и только потом встаёт на место старой: прервись копирование
    на середине — работающая версия останется нетронутой. Переименование папки
    не рвёт открытые файлы: процесс держит их по inode, а не по имени.
    """
    bundle = app_bundle(current_path or current_exe_path())
    if not bundle:
        raise ManualInstallRequired(
            "Программа запущена не из RetailCore.app. Перетащите новую версию "
            "из открывшегося образа в «Программы» сами.")
    parent = os.path.dirname(bundle)
    if not os.access(parent, os.W_OK):
        raise ManualInstallRequired(
            f"Нет прав на запись в «{parent}» — заменить программу может только "
            "её владелец. Перетащите RetailCore из открывшегося образа "
            "в «Программы».")

    mount_point = tempfile.mkdtemp(prefix="RetailCore-update-")
    staged = bundle + ".new"
    try:
        _run("hdiutil", "attach", "-nobrowse", "-readonly",
             "-mountpoint", mount_point, dmg_path)
        source = os.path.join(mount_point, os.path.basename(bundle))
        if not os.path.isdir(source):
            raise ManualInstallRequired(
                f"В образе обновления нет {os.path.basename(bundle)}. "
                "Установите новую версию вручную.")
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(source, staged, symlinks=True)
    finally:
        _run("hdiutil", "detach", mount_point, check=False)
        shutil.rmtree(mount_point, ignore_errors=True)

    previous = bundle + ".old"
    shutil.rmtree(previous, ignore_errors=True)
    os.rename(bundle, previous)
    os.rename(staged, bundle)
    shutil.rmtree(previous, ignore_errors=True)
    # `open -n` поднимает ещё одну копию, не спрашивая LaunchServices о уже
    # запущенной: та через мгновение закроется, и ждать её здесь нельзя.
    subprocess.Popen(["open", "-n", bundle], env=child_environment(),
                     close_fds=True)


def _run(*command: str, check: bool = True) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if check and result.returncode:
        raise OSError(f"{' '.join(command[:2])}: "
                      f"{result.stderr.strip() or result.returncode}")


def _install_windows(new_exe_path: str, current_path: str | None = None) -> None:
    """Подменяет запущенный exe и перезапускает приложение под тем же именем.

    Windows позволяет переименовать exe-файл работающего процесса (в отличие
    от удаления), поэтому отдельный updater-процесс не нужен: переименовываем
    старый файл, ставим новый на его место и запускаем.
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
