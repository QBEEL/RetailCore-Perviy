"""Печать готовых страниц PDF как отдельных этикеток."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QMarginsF, QRectF, QSize, QSizeF
from PySide6.QtGui import QPageLayout, QPageSize, QPainter
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPrintSupport import QPrinter

from ..core.marking.labels import LabelSpec
from . import label_print


LABEL_SIZE_MM = QSizeF(LabelSpec().width, LabelSpec().height)


def open_labels(path: str) -> tuple[QPdfDocument, QSizeF]:
    document = QPdfDocument()
    if document.load(path) != QPdfDocument.Error.None_ or document.pageCount() == 0:
        raise label_print.PrintError("Не удалось открыть PDF с этикетками.")
    size = document.pagePointSize(0)
    if size.width() <= 0 or size.height() <= 0:
        raise label_print.PrintError("В PDF указан неверный размер страницы.")
    for index in range(1, document.pageCount()):
        other = document.pagePointSize(index)
        if abs(other.width() - size.width()) > 0.1 or abs(other.height() - size.height()) > 0.1:
            raise label_print.PrintError(
                "В PDF страницы разного размера. Для печати этикеток нужен один размер.")
    return document, size


def print_pdf(document: QPdfDocument, size: QSizeF, printer_name: str,
              progress: Callable[[int, int], bool] | None = None) -> int:
    """Вписывает страницу PDF в этикетку маркировки 43 × 25 мм."""
    if printer_name not in label_print.printers():
        raise label_print.PrintError(f"Принтер «{printer_name or 'не выбран'}» не найден.")
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setPrinterName(printer_name)
    printer.setPageSize(QPageSize(LABEL_SIZE_MM, QPageSize.Unit.Millimeter))
    printer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
    printer.setFullPage(True)
    printer.setColorMode(QPrinter.ColorMode.GrayScale)
    painter = QPainter()
    if not painter.begin(printer):
        raise label_print.PrintError("Принтер не принял задание. Проверьте принтер и очередь печати.")
    done = 0
    try:
        count = document.pageCount()
        # 300 dpi сохраняет читаемость мелких кодов и ограничивает расход памяти.
        pixels = QSize(round(size.width() * 300 / 72),
                       round(size.height() * 300 / 72))
        if pixels.isEmpty():
            raise label_print.PrintError("В PDF указан неверный размер страницы.")
        page = printer.pageRect(QPrinter.Unit.DevicePixel)
        scale = min(page.width() / size.width(), page.height() / size.height())
        width, height = size.width() * scale, size.height() * scale
        target = QRectF(page.x() + (page.width() - width) / 2,
                        page.y() + (page.height() - height) / 2, width, height)
        for index in range(count):
            image = document.render(index, pixels)
            if image.isNull():
                raise label_print.PrintError(f"Не удалось прочитать страницу PDF {index + 1}.")
            if index and not printer.newPage():
                raise label_print.PrintError("Принтер перестал принимать этикетки.")
            painter.drawImage(target, image)
            done = index + 1
            if progress is not None and progress(done, count) is False:
                break
    finally:
        painter.end()
    return done
