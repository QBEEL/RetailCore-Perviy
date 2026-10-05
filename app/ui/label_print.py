"""Печать этикеток с кодами маркировки на принтер этикеток.

Печать идёт через драйвер Windows (`QPrinter`), а не прямыми командами принтера:
так работает любая модель — у отдела это TSC TE310, — и не нужно знать, на каком
языке (TSPL, ZPL) она понимает команды. Символ DataMatrix рисуется квадратиками
целого числа точек: дробный размер модуля размывает края, и сканер не читает.

Один и тот же код рисует этикетку на принтере, в PDF и в предпросмотре, поэтому
на экране — ровно то, что напечатается.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QMarginsF, QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPageLayout, QPageSize, QPainter
from PySide6.QtPrintSupport import QPrinter, QPrinterInfo

from ..core.marking import codes as codes_module
from ..core.marking import datamatrix
from ..core.marking.labels import LabelSpec

# Точек на миллиметр у принтеров на 203 dpi — их большинство.
DEFAULT_DPMM = 8.0

# Образец для пробной этикетки и предпросмотра. Настоящий код тратить на проверку
# макета нельзя: он выдан безвозвратно.
SAMPLE_CODE = ("0104600000000007" "21Xy7Q2aB9pLm4"
               "\x1d91EE11\x1d92dGVzdGRhdGFmb3JsYWJlbHByZXZpZXd0ZXN0ZGF0YQ==")


class PrintError(RuntimeError):
    """Печать не состоялась. Текст пригоден для показа."""


def printers() -> list[str]:
    return [item.printerName() for item in QPrinterInfo.availablePrinters()]


def default_printer() -> str:
    return QPrinterInfo.defaultPrinter().printerName()


# По чему узнаётся принтер этикеток. Офисный принтер по умолчанию на этикетки
# не печатает, и первое, что видит человек, должен быть нужный.
_LABEL_MARKS = ("tsc", "zebra", "godex", "argox", "xprinter", "tspl", "zpl",
                "label", "этикет", "te210", "te310", "ttp-", "gk420", "zd4")


def suggested_printer() -> str:
    """Принтер этикеток, если такой есть, иначе принтер по умолчанию."""
    for item in QPrinterInfo.availablePrinters():
        text = f"{item.printerName()} {item.makeAndModel()}".lower()
        if any(mark in text for mark in _LABEL_MARKS):
            return item.printerName()
    return default_printer()


def draw_label(painter: QPainter, spec: LabelSpec, code: str, name: str,
               dpmm_x: float, dpmm_y: float) -> None:
    """Рисует одну этикетку в координатах устройства (точки)."""
    painter.fillRect(QRectF(0, 0, spec.width * dpmm_x, spec.height * dpmm_y),
                     QColor("white"))
    painter.setPen(QColor("black"))
    painter.setBrush(QColor("black"))

    matrix = datamatrix.encode(code)
    side = len(matrix)
    box = spec.dm_size * min(dpmm_x, dpmm_y)
    module = max(1, int(box // side))
    left = round((spec.dm_left + spec.offset_x) * dpmm_x)
    top = round((spec.dm_top + spec.offset_y) * dpmm_y)
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                painter.fillRect(left + x * module, top + y * module,
                                 module, module, QColor("black"))

    if spec.show_name and name.strip():
        _text(painter, spec, name.strip(), spec.name_left, spec.name_top,
              spec.name_width, spec.name_height, spec.name_font, dpmm_x, dpmm_y,
              Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    if spec.show_text:
        parsed = codes_module.parse(code)
        line_height = spec.text_font * 25.4 / 72 * 1.25
        _text(painter, spec, parsed.gtin or "", spec.text_left, spec.text_top,
              spec.text_width, line_height, spec.text_font, dpmm_x, dpmm_y,
              Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        _text(painter, spec, parsed.serial or "", spec.text_left,
              spec.text_top + line_height, spec.text_width, line_height,
              spec.text_font, dpmm_x, dpmm_y,
              Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def _text(painter: QPainter, spec: LabelSpec, text: str, left: float, top: float,
          width: float, height: float, size: float, dpmm_x: float, dpmm_y: float,
          align: Qt.AlignmentFlag) -> None:
    if not text:
        return
    font = QFont("Arial")
    # Размер в точках устройства: так текст не зависит от разрешения экрана.
    font.setPixelSize(max(6, round(size * 25.4 / 72 * dpmm_y)))
    painter.setFont(font)
    rect = QRectF((left + spec.offset_x) * dpmm_x, (top + spec.offset_y) * dpmm_y,
                  width * dpmm_x, height * dpmm_y)
    painter.drawText(rect, int(align | Qt.TextFlag.TextWordWrap), text)


def preview(spec: LabelSpec, code: str = SAMPLE_CODE, name: str = "Название товара",
            dpmm: float = DEFAULT_DPMM, zoom: int = 1) -> QImage:
    """Картинка этикетки такой, какой она выйдет с принтера на 203 dpi."""
    image = QImage(round(spec.width * dpmm), round(spec.height * dpmm),
                   QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    try:
        draw_label(painter, spec, code, name, dpmm, dpmm)
    finally:
        painter.end()
    if zoom > 1:
        return image.scaled(image.width() * zoom, image.height() * zoom,
                            Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.FastTransformation)
    return image


def _printer(spec: LabelSpec, printer_name: str, pdf_path: str) -> QPrinter:
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    if pdf_path:
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(pdf_path)
    else:
        if printer_name not in printers():
            raise PrintError(
                f"Принтер «{printer_name or 'не выбран'}» не найден. Проверьте, "
                "что он включён и установлен в Windows.")
        printer.setPrinterName(printer_name)
    size = QPageSize(QSizeF(spec.width, spec.height), QPageSize.Unit.Millimeter,
                     "Этикетка")
    printer.setPageSize(size)
    printer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
    printer.setFullPage(True)
    printer.setColorMode(QPrinter.ColorMode.GrayScale)
    return printer


def print_codes(codes: list[str], name: str, spec: LabelSpec, printer_name: str,
                *, pdf_path: str = "",
                progress: Callable[[int, int], bool] | None = None) -> int:
    """Печатает этикетки по порядку и возвращает, сколько ушло на принтер.

    `progress(готово, всего)` вызывается после каждой этикетки; если он вернул
    `False`, печать останавливается — человек нажал «Отмена». Возвращаемое число
    нужно вызвавшему, чтобы запомнить, откуда продолжать.
    """
    if not codes:
        return 0
    if problems := spec.problems:
        raise PrintError("Макет этикетки не годится: " + "; ".join(problems) + ".")
    # Ошибка кодировки одного кода не должна остановить печать на середине
    # пачки: проверяем всё до первой этикетки.
    for code in codes:
        try:
            datamatrix.encode(code)
        except datamatrix.DataMatrixError as error:
            raise PrintError(f"Код не помещается в DataMatrix: {error}") from None

    printer = _printer(spec, printer_name, pdf_path)
    painter = QPainter()
    if not painter.begin(printer):
        raise PrintError(
            "Принтер не принял задание. Проверьте, что он включён, в нём есть "
            "этикетки и в очереди печати Windows нет зависших заданий.")
    done = 0
    try:
        dpmm_x = printer.logicalDpiX() / 25.4
        dpmm_y = printer.logicalDpiY() / 25.4
        for index, code in enumerate(codes):
            if index:
                if not printer.newPage():
                    raise PrintError("Принтер перестал принимать этикетки.")
            draw_label(painter, spec, code, name, dpmm_x, dpmm_y)
            done = index + 1
            if progress is not None and progress(done, len(codes)) is False:
                break
    finally:
        painter.end()
    return done
