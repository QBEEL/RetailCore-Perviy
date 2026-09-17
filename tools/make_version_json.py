"""Манифест релиза для автообновления.

Приложение проверяет обновления через GitHub Releases и ждёт рядом с exe файл
`version.json`: из него берутся контрольная сумма скачиваемого файла и список
изменений для окна обновления. Без манифеста проверка обрывается на KeyError,
пользователь видит «Неожиданный ответ GitHub» и остаётся на старой версии —
поэтому файл собирается в CI, а не выкладывается руками.

Версия берётся из `app/__init__.py`, а список изменений — из раздела этой же
версии в CHANGELOG.md. Передавать их аргументами значило бы держать номер
версии в трёх местах и однажды выложить манифест от предыдущей сборки.

Запуск:  python tools/make_version_json.py <exe> <CHANGELOG.md> <куда>
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

_VERSION_RE = re.compile(r'__version__\s*=\s*"([^"]+)"')
ROOT = Path(__file__).resolve().parents[1]


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
    if not changelog.exists():
        return []
    lines: list[str] = []
    inside = False
    for line in changelog.read_text("utf-8").splitlines():
        if line.startswith("## "):
            # Раздел версии начинается здесь и кончается следующим таким же.
            if inside:
                break
            inside = line[3:].strip().lstrip("vV") == version
            continue
        if inside and (stripped := line.strip().lstrip("-*").strip()):
            lines.append(stripped)
    return lines


def build(exe: Path, changelog: Path, target: Path, *, root: Path | None = None) -> dict:
    version = app_version(root)
    manifest = {
        "version": version,
        # Обязательность обновления решает человек, а не сборка: пометка
        # запирает работу до установки новой версии.
        "mandatory": False,
        "changelog": changelog_for(version, changelog),
        "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
    }
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return manifest


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    exe, changelog, target = (Path(argument) for argument in sys.argv[1:])
    if not exe.is_file():
        print(f"Не найден файл сборки: {exe}")
        return 1
    manifest = build(exe, changelog, target)
    print(f"версия: {manifest['version']}")
    print(f"sha256: {manifest['sha256']}")
    print(f"changelog: {len(manifest['changelog'])} строк")
    if not manifest["changelog"]:
        # Не ошибка, но в окне обновления людям будет нечего прочитать.
        print(f"ВНИМАНИЕ: в {changelog} нет раздела «## {manifest['version']}»")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
