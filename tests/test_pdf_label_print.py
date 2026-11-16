"""Готовые PDF-этикетки: размер и порядок страниц при печати."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSizeF
from PySide6.QtGui import QPageSize, QPainter, QPdfWriter
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QApplication

from app.ui import label_print, pdf_label_print


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_print_pdf_keeps_page_count_and_size(tmp_path, monkeypatch, app):
    source = tmp_path / "source.pdf"
    target = tmp_path / "printed.pdf"
    writer = QPdfWriter(str(source))
    writer.setPageSize(QPageSize(QSizeF(56, 33), QPageSize.Unit.Millimeter))
    painter = QPainter(writer)
    painter.drawText(20, 40, "first")
    assert writer.newPage()
    painter.drawText(20, 40, "second")
    painter.end()
    del painter, writer

    document, size = pdf_label_print.open_labels(str(source))
    assert document.pageCount() == 2
    assert abs(size.width() * 25.4 / 72 - 56) < 0.2

    class PdfPrinter(QPrinter):
        def __init__(self, mode):
            super().__init__(mode)
            self.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            self.setOutputFileName(str(target))

        def setPrinterName(self, name):
            pass

    monkeypatch.setattr(pdf_label_print, "QPrinter", PdfPrinter)
    monkeypatch.setattr(label_print, "printers", lambda: ["test"])
    try:
        assert pdf_label_print.print_pdf(document, size, "test") == 2
    finally:
        document.close()
    printed, printed_size = pdf_label_print.open_labels(str(target))
    try:
        assert printed.pageCount() == 2
        assert abs(printed_size.width() * 25.4 / 72 - 43) < 0.2
        assert abs(printed_size.height() * 25.4 / 72 - 25) < 0.2
    finally:
        printed.close()


def test_open_labels_rejects_invalid_pdf(tmp_path, app):
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"not a pdf")
    with pytest.raises(label_print.PrintError, match="Не удалось открыть PDF"):
        pdf_label_print.open_labels(str(source))
