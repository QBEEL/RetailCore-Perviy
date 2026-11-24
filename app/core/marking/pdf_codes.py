"""Чтение текстовых кодов и Data Matrix с этикеток в PDF."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from pdfminer.high_level import extract_text

from . import codes, image_codes


class PdfCodesProblem(ValueError):
    """PDF не содержит пригодных для импорта кодов."""


@dataclass
class FullScan:
    marks: list[str]
    missing_crypto: int
    pages_without_marks: int


def read(path: str) -> list[str]:
    """Возвращает КИ из текста PDF; изображения и векторные DataMatrix не угадывает."""
    try:
        text = extract_text(path)
    except Exception as error:  # pdfminer использует несколько классов ошибок
        raise PdfCodesProblem(f"Не удалось прочитать PDF: {error}") from error
    found: list[str] = []
    # pdfminer разделяет текстовые блоки пустой строкой, а страницы — FF.
    # На этикетке ЧЗ часть серийного номера может перейти на вторую строку.
    for block in re.split(r"\r?\n[ \t]*\r?\n|\f", text):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) == 2 and (joined := _wrapped_code(*lines)):
            found.append(joined)
            continue
        for line in lines:
            raw = codes.from_keyboard(line)
            parsed = codes.parse(raw)
            if parsed.valid and parsed.ki:
                found.append(raw)
    if not found:
        raise PdfCodesProblem(
            "В PDF не найдено текстовых кодов маркировки. "
            "Для PDF, где марки сохранены только рисунками, нужна выгрузка CSV из ЧЗ."
        )
    return found


def _wrapped_code(first: str, second: str) -> str:
    """Соединяет КИ с перенесённым 13-символьным серийным номером."""
    first_code = codes.parse(first)
    if not first_code.valid or not first_code.ki or first_code.full or len(first_code.serial) >= 13:
        return ""
    if not re.fullmatch(r"[\x21-\x7e]+", second) or len(first_code.serial) + len(second) != 13:
        return ""
    joined = codes.from_keyboard(first + second)
    code = codes.parse(joined)
    return joined if code.valid and len(code.serial) == 13 and not code.full else ""


def read_full(path: str) -> list[str]:
    """Читает полные марки. Сверяет их с текстовыми подписями, если они есть."""
    result = scan_full(path)
    if not result.marks:
        raise PdfCodesProblem("В PDF не найдено марок с криптохвостом.")
    if result.missing_crypto:
        raise PdfCodesProblem(
            f"Не удалось считать криптохвост для {result.missing_crypto} кодов из подписей в PDF. "
            "Используйте PDF с читаемыми Data Matrix или исходный CSV с полными марками."
        )
    return result.marks


def scan_full(path: str) -> FullScan:
    """Читает полные марки и возвращает частичный результат, если есть пропуски."""
    from PySide6.QtCore import QSize
    from PySide6.QtPdf import QPdfDocument

    document = QPdfDocument()
    if document.load(path) is not QPdfDocument.Error.None_:
        raise PdfCodesProblem("Не удалось открыть PDF для считывания Data Matrix.")
    found: list[str] = []
    pages_without_marks = 0
    for page in range(document.pageCount()):
        size = document.pagePointSize(page)
        longest_side = max(size.width(), size.height())
        if longest_side <= 0:
            pages_without_marks += 1
            continue
        scale = min(300 / 72, 4200 / longest_side)
        pixels = QSize(max(1, round(size.width() * scale)), max(1, round(size.height() * scale)))
        image = document.render(page, pixels)
        if image.isNull():
            pages_without_marks += 1
            continue
        before = len(found)
        for raw in image_codes.decode(image):
            if _with_crypto(codes.parse(raw)):
                found.append(raw)
        if len(found) == before:
            pages_without_marks += 1

    try:
        labels = [codes.parse(raw) for raw in read(path)]
    except PdfCodesProblem:
        labels = []
    decoded = Counter(codes.parse(raw).ki for raw in found)
    for label in labels:
        if decoded[label.ki] > 0:
            decoded[label.ki] -= 1
        elif _with_crypto(label):
            found.append(label.full)
        else:
            decoded[label.ki] -= 1
    missing = -sum(count for count in decoded.values() if count < 0)
    return FullScan(found, missing, pages_without_marks)


def _with_crypto(code: codes.Code) -> bool:
    return code.valid and bool(code.ki) and (
        (len(code.fields.get("91", "")) == 4 and bool(code.fields.get("92")))
        or len(code.fields.get("93", "")) == 4
    )
