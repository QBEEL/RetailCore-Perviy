"""Печать ценников-этикеток 30×20 на термопринтер.

Как и печать кодов маркировки, идёт через драйвер Windows: принтер любой
модели, не нужно знать его язык команд. Штрихкод рисуется целым числом точек на
модуль — дробная ширина размывает края, и сканер на кассе не читает. Один и тот
же код рисует этикетку на принтере, в PDF и в предпросмотре.
"""
from __future__ import annotations

import re
from typing import Callable

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter

from ..core import price_labels
from ..core.price_label_spec import PriceLabelSpec
from ..core.price_labels import PriceLabel
from . import label_print
from .label_print import DEFAULT_DPMM, PrintError

_WRAP = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap)


def _px(points: float, dpmm: float) -> int:
    """Размер шрифта в точках устройства: текст не зависит от разрешения экрана."""
    return max(5, round(points * 25.4 / 72 * dpmm))


def _font(pixels: int, bold: bool = False) -> QFont:
    font = QFont("Arial")
    font.setPixelSize(pixels)
    font.setBold(bold)
    return font


def barcode_scale(code: str, spec: PriceLabelSpec, dpmm_x: float) -> int:
    """Точек на модуль штрихкода; 0 — штрихкод шире этикетки."""
    modules = price_labels.encode(code).modules
    room = (spec.width - 2 * spec.margin) * dpmm_x
    return int(room // modules)


def fits(label: PriceLabel, spec: PriceLabelSpec, dpmm: float = DEFAULT_DPMM) -> bool:
    return barcode_scale(label.barcode, spec, dpmm) >= 1


def draw_label(painter: QPainter, spec: PriceLabelSpec, label: PriceLabel | None,
               dpmm_x: float, dpmm_y: float) -> None:
    """Рисует одну этикетку в координатах устройства (точки).

    `None` — пустая этикетка-разделитель: белый лист без единой точки.
    """
    painter.fillRect(QRectF(0, 0, spec.width * dpmm_x, spec.height * dpmm_y),
                     QColor("white"))
    if label is None:
        return
    painter.setPen(QColor("black"))
    painter.setBrush(QColor("black"))
    place = spec.layout()

    def x(mm: float) -> float:
        return (mm + spec.offset_x) * dpmm_x

    def y(mm: float) -> float:
        return mm * dpmm_y

    inner = spec.width - 2 * spec.margin

    _draw_name(painter, spec, label, x(spec.margin), y(place.name_top),
               inner * dpmm_x, place.name_height * dpmm_y, dpmm_y)

    if label.price is not None:
        row = QRectF(x(spec.margin), y(place.price_top), inner * dpmm_x,
                     spec.price_line * dpmm_y)
        if spec.prices == 2 and label.price2 is not None:
            _draw_two_prices(painter, spec, label, row, dpmm_y)
        else:
            painter.setFont(_font(_px(spec.price_font, dpmm_y), bold=True))
            painter.drawText(
                row, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                price_labels.format_price(label.price) + spec.price_suffix)

    code = price_labels.encode(label.barcode)
    scale = barcode_scale(label.barcode, spec, dpmm_x)
    if scale < 1:
        raise PrintError(
            f"Штрихкод {label.barcode} не помещается на этикетке {spec.width:g} мм: "
            "он слишком длинный.")
    width = code.modules * scale
    left = round(x(spec.margin) + (inner * dpmm_x - width) / 2)
    top = round(y(place.bar_top))
    height = round(place.bar_height * dpmm_y)
    for start, run in code.bars():
        painter.fillRect(left + start * scale, top, run * scale, height, QColor("black"))

    if spec.show_digits:
        painter.setFont(_font(_px(spec.digits_font, dpmm_y)))
        painter.drawText(
            QRectF(x(spec.margin), y(place.digits_top), inner * dpmm_x,
                   spec.digits_line * dpmm_y),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
            label.barcode)


def price_lines(spec: PriceLabelSpec, label: PriceLabel) -> tuple[str, str]:
    """Тексты двух строк цены: подпись, цена и знак после неё."""
    def line(caption: str, price) -> str:
        return f"{caption} {price_labels.format_price(price)}{spec.price_suffix}".strip()

    return line(spec.price_caption, label.price), line(spec.discount_caption, label.price2)


def _draw_two_prices(painter: QPainter, spec: PriceLabelSpec, label: PriceLabel,
                     row: QRectF, dpmm_y: float) -> None:
    """Две строки цены: «Цена: 469» и «Цена с макс. скидкой: 422».

    Строки одного размера, слева, как на образце отдела. Подпись со скидкой
    длинная, поэтому шрифт уменьшается, пока обе строки не влезут в ширину.
    """
    lines = price_lines(spec, label)
    size = _px(spec.price_font, dpmm_y)
    while True:
        painter.setFont(_font(size))
        widest = max(painter.fontMetrics().horizontalAdvance(text) for text in lines)
        if widest <= row.width() or size <= 5:
            break
        size -= 1
    half = row.height() / 2
    for index, text in enumerate(lines):
        painter.drawText(
            QRectF(row.left(), row.top() + index * half, row.width(), half),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)


def _draw_name(painter: QPainter, spec: PriceLabelSpec, label: PriceLabel,
               left: float, top: float, width: float, height: float, dpmm_y: float) -> None:
    """Название и характеристика. Шрифт уменьшается, пока всё не поместится."""
    if not (label.name or label.variant):
        return
    # «250 г» не рвётся на две строки: одинокая «г» внизу выглядит ошибкой.
    name = re.sub(r"(?<=\d) (?=[^\W\d]{1,3}\.?$)", "\u00a0", label.name)
    size = _px(spec.name_font, dpmm_y)
    smallest = _px(spec.name_font_min, dpmm_y)
    painter.save()
    painter.setClipRect(QRectF(left, top, width, height))
    while True:
        regular, bold = _font(size), _font(size, bold=True)
        painter.setFont(regular)
        name_height = (painter.boundingRect(QRectF(0, 0, width, 10_000), _WRAP,
                                            name).height() if name else 0)
        painter.setFont(bold)
        variant_height = (painter.boundingRect(QRectF(0, 0, width, 10_000), _WRAP,
                                               label.variant).height()
                          if label.variant else 0)
        if name_height + variant_height <= height or size <= smallest:
            break
        size -= 1
    if name:
        painter.setFont(regular)
        painter.drawText(QRectF(left, top, width, height), _WRAP, name)
    if label.variant:
        painter.setFont(bold)
        painter.drawText(QRectF(left, top + name_height, width, height - name_height),
                         _WRAP, label.variant)
    painter.restore()


def preview(spec: PriceLabelSpec, label: PriceLabel, dpmm: float = DEFAULT_DPMM,
            zoom: int = 1) -> QImage:
    """Картинка этикетки такой, какой она выйдет с принтера на 203 dpi."""
    image = QImage(round(spec.width * dpmm), round(spec.height * dpmm),
                   QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    try:
        draw_label(painter, spec, label, dpmm, dpmm)
    finally:
        painter.end()
    if zoom > 1:
        return image.scaled(image.width() * zoom, image.height() * zoom,
                            Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.FastTransformation)
    return image


def total_copies(labels: list[PriceLabel]) -> int:
    return sum(item.copies for item in labels if item.printable)


# Вызывается между номенклатурами в режиме «ждать, пока оторвут»: что напечатано,
# что следующее. Ложь — человек остановил печать.
Pause = Callable[[PriceLabel, PriceLabel], bool]


def print_labels(labels: list[PriceLabel], spec: PriceLabelSpec, printer_name: str,
                 *, pdf_path: str = "",
                 progress: Callable[[int, int], bool] | None = None,
                 blank_between: bool = False,
                 pause: Pause | None = None) -> int:
    """Печатает все годные позиции по порядку, каждую `copies` раз.

    Возвращает, сколько этикеток ушло на принтер (пустые разделители не в счёт).
    `progress(готово, всего)` вызывается после каждой этикетки; `False`
    останавливает печать — человек нажал «Остановить».

    `blank_between` — после каждой номенклатуры, кроме последней, идёт пустая
    этикетка: по ней видно, где рвать рулон.

    `pause` — каждая номенклатура уходит на принтер отдельным заданием, а перед
    следующей вызывается `pause`: пока человек не оторвал напечатанное, следующее
    в очередь не ставится. Одним заданием так нельзя: драйвер успел бы
    допечатать всё, пока человек рвёт первую пачку. В PDF смысла нет — рвать там
    нечего, — поэтому с `pdf_path` пауза не вызывается.
    """
    queue = [item for item in labels if item.printable]
    if not queue:
        return 0
    if problems := spec.problems:
        raise PrintError("Макет этикетки не годится: " + "; ".join(problems) + ".")

    groups: list[list[PriceLabel | None]] = []
    for index, item in enumerate(queue):
        group: list[PriceLabel | None] = [item] * item.copies
        if blank_between and index < len(queue) - 1:
            group.append(None)
        groups.append(group)
    jobs = groups if pause is not None and not pdf_path else [
        [page for group in groups for page in group]]
    total = sum(len(job) for job in jobs)

    first = label_print.make_printer(spec, printer_name, pdf_path)
    dpmm_x = first.logicalDpiX() / 25.4
    dpmm_y = first.logicalDpiY() / 25.4
    # Штрихкод, не помещающийся на этикетке, находим до задания, а не на
    # середине рулона: иначе в очередь уйдёт начатое и оборванное.
    for item in queue:
        if barcode_scale(item.barcode, spec, dpmm_x) < 1:
            raise PrintError(
                f"Штрихкод {item.barcode} (строка {item.row}) не помещается на "
                f"этикетке {spec.width:g} мм: он слишком длинный.")

    pages_done = 0
    printed = 0
    for number, job in enumerate(jobs):
        printer = first if number == 0 else label_print.make_printer(
            spec, printer_name, pdf_path)
        painter = QPainter()
        if not painter.begin(printer):
            raise PrintError(
                "Принтер не принял задание. Проверьте, что он включён, в нём есть "
                "этикетки и в очереди печати Windows нет зависших заданий.")
        stopped = False
        try:
            for position, page in enumerate(job):
                if position and not printer.newPage():
                    raise PrintError("Принтер перестал принимать этикетки.")
                draw_label(painter, spec, page, dpmm_x, dpmm_y)
                pages_done += 1
                printed += page is not None
                if progress is not None and progress(pages_done, total) is False:
                    stopped = True
                    break
        finally:
            painter.end()
        if stopped:
            return printed
        if pause is not None and not pdf_path and number < len(jobs) - 1:
            if not pause(queue[number], queue[number + 1]):
                return printed
    return printed
