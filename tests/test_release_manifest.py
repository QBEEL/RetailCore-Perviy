"""Манифест автообновления: то, без чего релиз выглядит целым, но не работает.

Клиент требует рядом с exe файл `version.json` и берёт из него контрольную
сумму. Ошибка здесь не падает на сборке — она тихо оставляет отдел на старой
версии: в окне обновления человек видит «Неожиданный ответ GitHub», либо
скачанный файл отклоняется проверкой целостности.

Поэтому проверяется и содержимое манифеста, и разбор CHANGELOG.md: описание
берётся по номеру версии, и подтянуть раздел от предыдущей сборки нельзя.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import make_version_json as manifest

CHANGELOG = """# Что нового

## 3.3.0

- Первый пункт новой версии
- Второй пункт новой версии

## 3.2.1

- Пункт прошлой версии
"""


def _changelog(tmp_path: Path) -> Path:
    target = tmp_path / "CHANGELOG.md"
    target.write_text(CHANGELOG, "utf-8")
    return target


def _app_package(tmp_path: Path, version: str) -> Path:
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text(
        f'"""Док."""\n\n__version__ = "{version}"\nAPP_TITLE = "RetailCore"\n', "utf-8")
    return tmp_path


# --- описание изменений -------------------------------------------------------------

def test_берётся_раздел_нужной_версии(tmp_path):
    lines = manifest.changelog_for("3.3.0", _changelog(tmp_path))

    assert lines == ["Первый пункт новой версии", "Второй пункт новой версии"]


def test_соседний_раздел_не_прилипает(tmp_path):
    """Иначе в окне обновления к новому описанию примешалось бы прошлое."""
    lines = manifest.changelog_for("3.2.1", _changelog(tmp_path))

    assert lines == ["Пункт прошлой версии"]


def test_версии_без_раздела_остаются_без_описания(tmp_path):
    """Не ошибка сборки: релиз без описания выпустить можно, без релиза — нет."""
    assert manifest.changelog_for("9.9.9", _changelog(tmp_path)) == []


def test_отсутствующий_файл_равен_пустому_описанию(tmp_path):
    assert manifest.changelog_for("3.3.0", tmp_path / "нет.md") == []


# --- версия -------------------------------------------------------------------------

def test_версия_читается_из_приложения(tmp_path):
    assert manifest.app_version(_app_package(tmp_path, "4.1.2")) == "4.1.2"


def test_версия_приложения_настоящая():
    """Тот же файл, что читает CI при сверке с тегом."""
    assert manifest.app_version().count(".") == 2


# --- манифест -----------------------------------------------------------------------

def test_манифест_несёт_сумму_версию_и_описание(tmp_path):
    exe = tmp_path / "RetailCore.exe"
    exe.write_bytes(b"\x00\x01build")
    target = tmp_path / "version.json"

    built = manifest.build(exe, _changelog(tmp_path), target,
                           root=_app_package(tmp_path, "3.3.0"))

    written = json.loads(target.read_text("utf-8"))
    assert written == built
    assert written["version"] == "3.3.0"
    assert written["sha256"] == hashlib.sha256(exe.read_bytes()).hexdigest()
    assert written["changelog"][0] == "Первый пункт новой версии"
    # Обязательность решает человек: пометка запирает работу до обновления.
    assert written["mandatory"] is False


def test_сумма_считается_по_самому_файлу_сборки(tmp_path):
    """Сумма от другого файла означает отказ обновления на стороне клиента."""
    exe = tmp_path / "RetailCore.exe"
    exe.write_bytes(b"first build")
    target = tmp_path / "version.json"
    root = _app_package(tmp_path, "3.3.0")
    first = manifest.build(exe, _changelog(tmp_path), target, root=root)

    exe.write_bytes(b"rebuilt differently")
    second = manifest.build(exe, _changelog(tmp_path), target, root=root)

    assert first["sha256"] != second["sha256"]
