"""Печать готовых страниц PDF как отдельных этикеток."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from pdfminer.high_level import extract_pages
from pdfminer.layout import LTComponent, LTFigure, LTImage, LTPage, LTRect, LTTextBox
from PySide6.QtCore import QBuffer, QIODevice, QMarginsF, QRectF, QSize, QSizeF
from PySide6.QtGui import QImage, QPageLayout, QPageSize, QPainter
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPrintSupport import QPrinter

from ..core.marking.labels import LabelSpec
from . import label_print


LABEL_SIZE_MM = QSizeF(LabelSpec().width, LabelSpec().height)


@dataclass(frozen=True)
class LabelRegion:
    page: int
    bounds: QRectF


@dataclass(frozen=True)
class LabelJob:
    data: bytes
    regions: list[LabelRegion]
    unsplit_sheets: int


def open_labels(source: str | bytes) -> tuple[QPdfDocument, QSizeF]:
    document = QPdfDocument()
    if isinstance(source, bytes):
        buffer = QBuffer(document)
        buffer.setData(source)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        document.load(buffer)
    else:
        document.load(source)
    if document.error() != QPdfDocument.Error.None_ or document.pageCount() == 0:
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


def prepare_labels(path: str) -> LabelJob:
    """Выделяет этикетки с отдельными рамками на многоэтикеточном листе."""
    data = Path(path).read_bytes()
    document, size = open_labels(data)
    try:
        count = document.pageCount()
    finally:
        document.close()

    page_bounds = QRectF(0, 0, size.width(), size.height())
    # Страницы, которые уже помещаются на ленту, печатаются целиком.
    page_mm = sorted((size.width() * 25.4 / 72, size.height() * 25.4 / 72))
    stock_mm = sorted((LABEL_SIZE_MM.width(), LABEL_SIZE_MM.height()))
    if all(page <= stock + 0.5 for page, stock in zip(page_mm, stock_mm)):
        return LabelJob(data, [LabelRegion(page, page_bounds) for page in range(count)], 0)

    regions = []
    unsplit = 0
    for page, layout in enumerate(extract_pages(BytesIO(data))):
        boxes = _label_boxes(layout, page_bounds)
        if not boxes:
            boxes = [page_bounds]
            unsplit += 1
        regions.extend(LabelRegion(page, bounds) for bounds in boxes)
    if not regions or regions[-1].page + 1 != count:
        raise label_print.PrintError("Не удалось разобрать все страницы PDF.")
    return LabelJob(data, regions, unsplit)


def _label_boxes(layout: LTPage, page_bounds: QRectF) -> list[QRectF]:
    # pdfminer отсчитывает координаты снизу, Qt — сверху.
    if abs(layout.width - page_bounds.width()) > 0.5 or abs(layout.height - page_bounds.height()) > 0.5:
        return []

    def bounds(item: LTComponent) -> QRectF:
        return QRectF(item.x0, layout.height - item.y1, item.width, item.height)

    frames = [bounds(item) for item in layout if isinstance(item, LTRect)
              and (item.stroke or item.fill) and item.width >= 25 and item.height >= 25]
    content = [bounds(item).center() for item in layout
               if isinstance(item, (LTFigure, LTImage, LTTextBox))
               or (isinstance(item, LTRect) and item.fill and item.width < 25 and item.height < 25)]
    if not content:
        return []
    # На этикетках одинакового размера рамки не пересекаются и покрывают текст
    # и рисунки. Фон листа и декоративные прямоугольники здесь не подходят.
    groups: dict[tuple[int, int], list[QRectF]] = {}
    for frame in frames:
        if not page_bounds.adjusted(-0.5, -0.5, 0.5, 0.5).contains(frame):
            continue
        if not any(frame.contains(point) for point in content):
            continue
        group = groups.setdefault((round(frame.width()), round(frame.height())), [])
        if frame not in group:
            group.append(frame)
    for group in sorted(groups.values(), key=len, reverse=True):
        if len(group) < 2 or not all(any(frame.contains(point) for frame in group) for point in content):
            continue
        if any(frame.adjusted(0.5, 0.5, -0.5, -0.5).intersects(other)
               for index, frame in enumerate(group) for other in group[:index]):
            continue
        return sorted(group, key=lambda frame: (round(frame.y()), frame.x()))
    return []


class LabelRenderer:
    """Хранит растровую копию только текущей страницы PDF."""

    def __init__(self, document: QPdfDocument) -> None:
        self.document = document
        self._page = -1
        self._image = QImage()

    def render(self, region: LabelRegion) -> QImage:
        size = self.document.pagePointSize(region.page)
        if region.page != self._page:
            scale = min(300 / 72, 5000 / max(size.width(), size.height()))
            pixels = QSize(max(1, round(size.width() * scale)), max(1, round(size.height() * scale)))
            page = self.document.render(region.page, pixels)
            if page.isNull():
                raise label_print.PrintError(f"Не удалось прочитать страницу PDF {region.page + 1}.")
            self._page, self._image = region.page, page
        x_scale = self._image.width() / size.width()
        y_scale = self._image.height() / size.height()
        rect = region.bounds
        pixels = QRectF(rect.x() * x_scale, rect.y() * y_scale,
                        rect.width() * x_scale, rect.height() * y_scale).toRect()
        return self._image.copy(pixels.intersected(self._image.rect()))


def draw_label(painter: QPainter, canvas: QRectF, image: QImage, size: QSizeF) -> None:
    scale = min(canvas.width() / size.width(), canvas.height() / size.height())
    width, height = size.width() * scale, size.height() * scale
    target = QRectF(canvas.x() + (canvas.width() - width) / 2,
                    canvas.y() + (canvas.height() - height) / 2, width, height)
    painter.drawImage(target, image)


def print_pdf(document: QPdfDocument, size: QSizeF, printer_name: str,
              progress: Callable[[int, int], bool] | None = None, *,
              regions: Sequence[LabelRegion] | None = None) -> int:
    """Вписывает каждую выбранную область PDF в этикетку 43 × 25 мм."""
    if regions is None:
        regions = [LabelRegion(page, QRectF(0, 0, size.width(), size.height()))
                   for page in range(document.pageCount())]
    if not regions:
        return 0
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
        page = printer.pageRect(QPrinter.Unit.DevicePixel)
        renderer = LabelRenderer(document)
        for index, region in enumerate(regions):
            image = renderer.render(region)
            if index and not printer.newPage():
                raise label_print.PrintError("Принтер перестал принимать этикетки.")
            draw_label(painter, page, image, region.bounds.size())
            done = index + 1
            if progress is not None and progress(done, len(regions)) is False:
                break
    finally:
        painter.end()
    return done
