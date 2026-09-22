"""Манифест релиза для автообновления.

Приложение проверяет обновления через GitHub Releases и ждёт рядом с exe файл
`version.json`: из него берутся контрольная сумма скачиваемого файла и список
изменений для окна обновления. Без манифеста проверка обрывается на KeyError,
пользователь видит «Неожиданный ответ GitHub» и остаётся на старой версии —
поэтому файл собирается в CI, а не выкладывается руками.

Версия берётся из `app/__init__.py`, а список изменений — из раздела этой же
версии в CHANGELOG.md. Передавать их аргументами значило бы держать номер
версии в трёх местах и однажды выложить манифест от предыдущей сборки.

Сумм в манифесте две: `sha256` для сборки Windows и `sha256_macos` для образа.
Одной не хватает — клиент сверяет контрольную сумму того файла, который скачал
сам, а файлы у платформ разные.

Запуск:  python tools/make_version_json.py <exe> <CHANGELOG.md> <куда> [dmg]
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

_VERSION_RE = re.compile(r'__version__\s*=\s*"([^"]+)"')
ROOT = Path(__file__).resolve().parents[1]

# Скрипт запускается как `python tools/make_version_json.py`: корень проекта
# в путь поиска не попадает, а разбор CHANGELOG общий с приложением.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import changelog as _changelog  # noqa: E402


def app_version(root: Path | None = None) -> str:
    """Версия приложения — та же строка, что показана в боковом меню."""
    source = (root or ROOT) / "app" / "__init__.py"
    match = _VERSION_RE.search(source.read_text("utf-8"))
    if not match:
        raise SystemExit(f"В {source} не найден __version__")
    return match.group(1)


def changelog_for(version: str, changelog: Path) -> list[str]:
    """Пункты раздела «## <версия>» из CHANGELOG.md.

    Отсутствующий раздел — не ошибка сборки: релиз лучше выпустить с пустым
    описанием, чем не выпустить вовсе. Пустой список окно обновления просто не
    покажет, а собранный exe от этого не хуже.
    """
    return _changelog.read(changelog, version)


def build(exe: Path, changelog: Path, target: Path, *,
          dmg: Path | None = None, root: Path | None = None) -> dict:
    version = app_version(root)
    manifest = {
        "version": version,
        # Обязательность обновления решает человек, а не сборка: пометка
        # запирает работу до установки новой версии.
        "mandatory": False,
        "changelog": changelog_for(version, changelog),
        # Ключ `sha256` закреплён за сборкой для Windows: его читают уже
        # выпущенные версии, и переносить их на другое имя нельзя — они
        # перестали бы обновляться. Образ macOS получает свой ключ.
        "sha256": _digest(exe),
    }
    if dmg is not None:
        manifest["sha256_macos"] = _digest(dmg)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return manifest


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def use_utf8_output() -> None:
    """Печать не должна зависеть от кодировки консоли.

    На windows-раннере GitHub Actions stdout — cp1252, и русская строка роняет
    процесс UnicodeEncodeError уже после записи манифеста: файл на месте и
    верен, а шаг сборки помечен упавшим, релиз не выходит. Та же беда ждёт
    запуск руками из cmd.exe, где кодировка cp866.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    use_utf8_output()
    if len(sys.argv) not in (4, 5):
        print(__doc__)
        return 2
    exe, changelog, target = (Path(argument) for argument in sys.argv[1:4])
    dmg = Path(sys.argv[4]) if len(sys.argv) == 5 else None
    for required in (exe, dmg):
        if required is not None and not required.is_file():
            print(f"Не найден файл сборки: {required}")
            return 1
    manifest = build(exe, changelog, target, dmg=dmg)
    print(f"версия: {manifest['version']}")
    print(f"sha256 windows: {manifest['sha256']}")
    if dmg is None:
        # macOS качает образ и сверяет сумму, которой в манифесте нет, —
        # обновление отвалится на проверке целостности у каждого.
        print("ВНИМАНИЕ: образ macOS не передан, ключа sha256_macos нет")
    else:
        print(f"sha256 macos:   {manifest['sha256_macos']}")
    print(f"changelog: {len(manifest['changelog'])} строк")
    if not manifest["changelog"]:
        # Не ошибка, но в окне обновления людям будет нечего прочитать.
        print(f"ВНИМАНИЕ: в {changelog} нет раздела «## {manifest['version']}»")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
