"""Считывание Data Matrix с изображений."""
from __future__ import annotations

from pathlib import Path

import zxingcpp
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage, QImageReader, QPainter, QPolygonF, QTransform

from . import codes


_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")
FILE_PATTERNS = " ".join(f"*{suffix}" for suffix in _EXTENSIONS)


def is_image(path: str) -> bool:
    return Path(path).suffix.lower() in _EXTENSIONS


def read(path: str) -> list[str]:
    """Читает фото с учётом ориентации камеры и сохраняет байты кодов."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        raise ValueError(f"Не удалось открыть изображение: {reader.errorString()}")
    marks = [raw for raw in decode(image, retry=True) if codes.parse(raw).valid]
    if not marks:
        raise ValueError(
            "На изображении не удалось считать коды маркировки. "
            "Попробуйте снять Data Matrix крупнее и без бликов."
        )
    return marks


def decode(image: QImage, *, retry: bool = False) -> list[str]:
    """Возвращает исходный текст Data Matrix, включая разделители GS."""
    if image.hasAlphaChannel():
        opaque = QImage(image.size(), QImage.Format.Format_RGB32)
        opaque.fill(Qt.GlobalColor.white)
        with QPainter(opaque) as painter:
            painter.drawImage(0, 0, image)
        image = opaque
    image = image.convertToFormat(QImage.Format.Format_Grayscale8)
    barcodes = _scan(image, return_errors=retry)
    if retry and not barcodes:
        barcodes = _scan(image, return_errors=True, binarizer=zxingcpp.Binarizer.GlobalHistogram)
    barcodes.sort(key=lambda barcode: (barcode.position.top_left.y, barcode.position.top_left.x))
    result = []
    for barcode in barcodes:
        if retry and not barcode.valid:
            barcode = _retry_symbol(image, barcode.position)
        if barcode is not None and barcode.valid and barcode.bytes.isascii():
            result.append(barcode.bytes.decode("ascii"))
    return result


def _scan(image: QImage, **options) -> list[zxingcpp.Barcode]:
    view = zxingcpp.ImageView(image.constBits(), image.width(), image.height(),
                              zxingcpp.ImageFormat.Lum, image.bytesPerLine())
    return zxingcpp.read_barcodes(view, formats=zxingcpp.BarcodeFormat.DataMatrix, **options)


def _retry_symbol(image: QImage, position: zxingcpp.Position) -> zxingcpp.Barcode | None:
    """Выравнивает найденный символ и повторяет чтение с разными порогами яркости."""
    source = QPolygonF([QPointF(point.x, point.y) for point in (
        position.top_left, position.top_right, position.bottom_right, position.bottom_left,
    )])
    left, right = 20, 236
    target = QPolygonF([QPointF(left, left), QPointF(right, left),
                        QPointF(right, right), QPointF(left, right)])
    transform = QTransform()
    if not QTransform.quadToQuad(source, target, transform):
        return None
    corrected = QImage(left + right, left + right, QImage.Format.Format_RGB32)
    corrected.fill(Qt.GlobalColor.white)
    with QPainter(corrected) as painter:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setTransform(transform)
        painter.drawImage(0, 0, image)
    corrected = corrected.convertToFormat(QImage.Format.Format_Grayscale8)
    pixels = bytes(corrected.constBits())
    for threshold in (85, 110, 135, 160, 185, 210):
        table = bytes(threshold) + b"\xff" * (256 - threshold)
        binary = QImage(pixels.translate(table), corrected.width(), corrected.height(),
                        corrected.bytesPerLine(), QImage.Format.Format_Grayscale8)
        found = _scan(binary, binarizer=zxingcpp.Binarizer.BoolCast)
        if found:
            return found[0]
    return None
