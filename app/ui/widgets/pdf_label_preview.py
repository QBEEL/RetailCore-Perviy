"""Предпросмотр готовых этикеток перед отправкой на принтер."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout,
    QWidget,
)

from .. import label_print, pdf_label_print


class PdfLabelPreview(QDialog):
    def __init__(self, document: QPdfDocument, job: pdf_label_print.LabelJob,
                 printer_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Предпросмотр этикеток")
        self._renderer = pdf_label_print.LabelRenderer(document)
        self._regions = job.regions
        self._printer_name = printer_name
        count = len(self._regions)

        root = QVBoxLayout(self)
        stock = pdf_label_print.LABEL_SIZE_MM
        summary = QLabel(f"Этикеток к печати: {count} · {stock.width():g} × {stock.height():g} мм", self)
        root.addWidget(summary)
        printer = QLabel(self)
        printer.setTextFormat(Qt.TextFormat.PlainText)
        printer.setText(f"Принтер: {printer_name}" if printer_name else
                        "Выберите принтер на вкладке «Этикетки», чтобы напечатать.")
        root.addWidget(printer)
        if job.unsplit_sheets:
            notice = QLabel(
                f"Страниц без распознанных рамок: {job.unsplit_sheets}. "
                "Они показаны целиком и займут по одной этикетке.", self)
            notice.setWordWrap(True)
            root.addWidget(notice)

        self.image = QLabel(self)
        self.image.setFixedSize(round(stock.width() * 12), round(stock.height() * 12))
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setWordWrap(True)
        root.addWidget(self.image)

        navigation = QHBoxLayout()
        self.previous = QPushButton("Назад", self)
        self.next = QPushButton("Вперёд", self)
        self.number = QSpinBox(self)
        self.number.setRange(1, count)
        self.number.setKeyboardTracking(False)
        self.number.setAccessibleName("Номер этикетки")
        self.previous.clicked.connect(self.number.stepDown)
        self.next.clicked.connect(self.number.stepUp)
        self.number.valueChanged.connect(self._show_label)
        navigation.addWidget(self.previous)
        navigation.addStretch()
        navigation.addWidget(QLabel("Этикетка", self))
        navigation.addWidget(self.number)
        navigation.addWidget(QLabel(f"из {count}", self))
        navigation.addStretch()
        navigation.addWidget(self.next)
        root.addLayout(navigation)

        buttons = QDialogButtonBox(self)
        cancel = buttons.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        self.print_button = buttons.addButton(
            "Напечатать" if count == 1 else "Напечатать все",
            QDialogButtonBox.ButtonRole.AcceptRole)
        self.print_button.setObjectName("Primary")
        for button in (self.previous, self.next, self.print_button):
            button.setAutoDefault(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        cancel.setDefault(True)
        cancel.setFocus()
        self._show_label(1)

    def _show_label(self, number: int) -> None:
        self.previous.setEnabled(number > 1)
        self.next.setEnabled(number < len(self._regions))
        region = self._regions[number - 1]
        try:
            source = self._renderer.render(region)
        except label_print.PrintError as error:
            self.image.setText(str(error))
            self.print_button.setEnabled(False)
            return
        canvas = QImage(self.image.size(), QImage.Format.Format_RGB32)
        canvas.fill(Qt.GlobalColor.white)
        with QPainter(canvas) as painter:
            pdf_label_print.draw_label(painter, QRectF(canvas.rect()), source, region.bounds.size())
        self.image.setPixmap(QPixmap.fromImage(canvas))
        self.print_button.setEnabled(bool(self._printer_name))
