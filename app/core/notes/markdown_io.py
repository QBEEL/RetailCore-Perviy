"""Перенос страницы в файл .md и обратно.

Страница хранится как Markdown, поэтому файл — это тот же текст. Заголовок в
хранилище отдельное поле, а в файле он первой строкой `# Заголовок`: так файл
открывается где угодно и выглядит как страница, а не как безымянный кусок текста.
"""
from __future__ import annotations

import os
import re

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_HEADING = re.compile(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$")


def file_name_for(title: str) -> str:
    """Имя файла по заголовку: без знаков, которые Windows не принимает в именах."""
    name = _FORBIDDEN.sub(" ", title).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return (name or "Без названия")[:80]


def to_file_text(title: str, body: str) -> str:
    title = title.strip()
    body = body.strip("\n")
    if not title:
        return body + "\n" if body else ""
    return f"# {title}\n\n{body}\n" if body else f"# {title}\n"


def from_file_text(text: str, fallback_title: str = "") -> tuple[str, str]:
    """Заголовок и текст страницы из файла.

    Первая непустая строка вида `# Заголовок` становится заголовком страницы и из
    текста убирается. Нет такой строки — заголовком служит имя файла.
    """
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is not None:
        match = _HEADING.match(lines[first].strip())
        if match:
            body = "\n".join(lines[first + 1:]).strip("\n")
            return match.group(1).strip(), body
    return fallback_title, text.strip("\n")


def title_from_path(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]
