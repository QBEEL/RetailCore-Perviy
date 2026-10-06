"""Имена и типы вложений.

Имя вложения приходит с сервера, а туда его присылает клиент в том виде, в
каком оно стоит в исходном файле: с путём, управляющими знаками, любым
расширением. В путь на диске идёт только приведённое к единому виду имя, и
только внутри папки своего вложения.

Типы, которые система запускает, а не показывает (`.exe`, скрипты, ярлыки),
как вложения не принимаются; уже лежащие в базе открываются после
подтверждения.

Тот же набор расширений и та же очистка есть на сервере
(`server/api/app/routers/files.py`): код клиента серверу недоступен, и держать
их в синхроне приходится вручную.
"""
from __future__ import annotations

import os

# Типы, которые система запускает, а не показывает.
RISKY_EXTENSIONS: frozenset[str] = frozenset({
    ".exe", ".com", ".scr", ".pif", ".msi", ".msp", ".dll", ".cpl", ".sys",
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
    ".wsh", ".hta", ".lnk", ".url", ".reg", ".jar", ".msc", ".chm", ".appx",
    ".gadget", ".application", ".sh", ".command", ".app", ".dmg", ".apk",
})

_FORBIDDEN = '<>:"|?*'
MAX_NAME = 150


def safe_name(raw: str | None, fallback: str = "file") -> str:
    """Имя файла без путей, управляющих знаков и запрещённых символов."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in _FORBIDDEN)
    name = name.strip(" .")
    if len(name) > MAX_NAME:
        root, ext = os.path.splitext(name)
        name = root[:MAX_NAME - len(ext)] + ext
    return name or fallback


def is_risky(name: str | None) -> bool:
    """Запускаемый тип — по последнему расширению, как его видит Windows.

    «счёт.pdf.exe» запускается как программа, а «счёт.exe.pdf» и
    «www.ozon.com.xlsx» открываются как документы: точка в середине имени —
    часть названия, и отказ по ней не пускал обычные файлы Excel.
    """
    return os.path.splitext(safe_name(name, "").lower())[1].strip() in RISKY_EXTENSIONS
