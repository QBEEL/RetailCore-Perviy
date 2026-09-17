"""Выпуск версии одной командой: версия в коде, коммит, тег.

Дальше всё делает CI: собирает Windows и macOS, считает контрольную сумму,
кладёт `version.json` рядом с exe и публикует релиз. Приложение подхватывает
его само при следующем запуске.

Проверки здесь не ради строгости. Тег, поставленный на грязное дерево, укажет
не на то, что собрано; версия, не совпавшая с тегом, заставит клиентов
обновляться по кругу; релиз без раздела в CHANGELOG.md оставит людей без
объяснения, что изменилось. Каждую из этих ошибок дешевле поймать до пуша, чем
разбирать по жалобам.

Запуск:  python tools/release.py 3.3.0 [--note "коротко о главном"] [--push]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.make_version_json import (  # noqa: E402
    app_version,
    changelog_for,
    use_utf8_output,
)

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "app" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
_VERSION_FORMAT = re.compile(r"^\d+\.\d+\.\d+$")


def git(*arguments: str, capture: bool = True) -> str:
    result = subprocess.run(("git", *arguments), cwd=ROOT, text=True,
                            capture_output=capture, check=True)
    return (result.stdout or "").strip()


def fail(message: str) -> None:
    raise SystemExit(f"Выпуск остановлен: {message}")


def check_clean() -> None:
    if git("status", "--porcelain"):
        fail("в рабочем дереве есть незакоммиченные изменения — тег указал бы "
             "не на то, что уйдёт в сборку.\nЗакоммитьте их или уберите в stash.")


def check_version(version: str) -> None:
    if not _VERSION_FORMAT.match(version):
        fail(f"версия «{version}» не вида 3.3.0")
    current = app_version(ROOT)
    if _parts(version) <= _parts(current):
        fail(f"версия {version} не новее текущей {current} — "
             "клиенты такое обновление не увидят")
    if git("tag", "--list", f"v{version}"):
        fail(f"тег v{version} уже существует")


def _parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def check_changelog(version: str) -> list[str]:
    lines = changelog_for(version, CHANGELOG)
    if not lines:
        fail(f"в {CHANGELOG.name} нет раздела «## {version}» — "
             "людям в окне обновления будет нечего прочитать")
    return lines


def write_version(version: str) -> None:
    source = VERSION_FILE.read_text("utf-8")
    VERSION_FILE.write_text(
        re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', source),
        "utf-8")


def main() -> int:
    # Сообщения здесь русские, а cmd.exe печатает в cp866: без этого скрипт
    # падал бы на выводе, успев поставить тег.
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="например 3.3.0")
    parser.add_argument("--note", default="", help="короткое описание для коммита")
    parser.add_argument("--push", action="store_true",
                        help="сразу отправить коммит и тег — сборка начнётся сама")
    options = parser.parse_args()
    version = options.version.lstrip("vV")

    check_clean()
    check_version(version)
    changes = check_changelog(version)

    print(f"RetailCore {version} · пунктов в описании: {len(changes)}")
    for line in changes:
        print(f"  · {line[:96]}")

    write_version(version)
    title = f"RetailCore {version}" + (f": {options.note}" if options.note else "")
    git("add", str(VERSION_FILE.relative_to(ROOT)))
    git("commit", "-m", title)
    git("tag", f"v{version}")
    print(f"\nкоммит и тег v{version} готовы")

    if options.push:
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        git("push", "origin", branch, capture=False)
        git("push", "origin", f"v{version}", capture=False)
        print("отправлено — сборка идёт: "
              "https://github.com/QBEEL/RetailCore-Perviy/actions")
        return 0

    print("осталось отправить — и сборка начнётся сама:\n"
          f"  git push origin HEAD && git push origin v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
