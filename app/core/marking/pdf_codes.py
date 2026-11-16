"""Чтение кодов маркировки из текстового PDF, выгруженного из СУЗ."""
from __future__ import annotations

from pdfminer.high_level import extract_text

from . import codes


class PdfCodesProblem(ValueError):
    """PDF не содержит пригодных для импорта текстовых кодов."""


def read(path: str) -> list[str]:
    """Возвращает КИ из текста PDF; изображения и векторные DataMatrix не угадывает."""
    try:
        text = extract_text(path)
    except Exception as error:  # pdfminer использует несколько классов ошибок
        raise PdfCodesProblem(f"Не удалось прочитать PDF: {error}") from error
    found: list[str] = []
    for line in text.splitlines():
        raw = codes.from_keyboard(line.strip())
        parsed = codes.parse(raw)
        if parsed.valid and parsed.ki:
            found.append(raw)
    if not found:
        raise PdfCodesProblem(
            "В PDF не найдено текстовых кодов маркировки. "
            "Для PDF, где марки сохранены только рисунками, нужна выгрузка CSV из ЧЗ."
        )
    return found
