"""Скачивает sing-box в vendor/sing-box для запуска из исходников и сборки.

Версия и сумма закреплены здесь: сборка не должна молча подхватить другой
файл, потому что он запускается на машинах сотрудников. Другая версия ставится
правкой двух констант ниже — сумма берётся со страницы релиза на GitHub.

Запуск:  python tools/fetch_singbox.py
"""
from __future__ import annotations

import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = "1.13.21"
SHA256 = "a03291793d3a3c6e266447a58140657ac099ff278abf3b8ff678932356a62ced"

URL = (f"https://github.com/SagerNet/sing-box/releases/download/v{VERSION}/"
       f"sing-box-{VERSION}-windows-amd64.zip")
TARGET = Path(__file__).resolve().parents[1] / "vendor" / "sing-box"
# libcronet.dll нужен только для исходящего канала naive, которого у нас нет.
WANTED = ("sing-box.exe", "LICENSE")


def main() -> int:
    if (TARGET / "sing-box.exe").is_file() and (TARGET / "VERSION").read_text().strip() == VERSION:
        print(f"sing-box {VERSION} уже на месте: {TARGET}")
        return 0
    print(f"Загрузка {URL}")
    with urllib.request.urlopen(URL, timeout=120) as response:
        payload = response.read()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != SHA256:
        print(f"Сумма не совпала: ожидалась {SHA256}, получена {actual}", file=sys.stderr)
        return 1
    TARGET.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.namelist():
            name = member.rsplit("/", 1)[-1]
            if name in WANTED:
                (TARGET / name).write_bytes(archive.read(member))
    (TARGET / "VERSION").write_text(VERSION, encoding="utf-8")
    print(f"Готово: {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
