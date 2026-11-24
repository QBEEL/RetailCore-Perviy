"""Готовые PDF-этикетки: размер и порядок страниц при печати."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSizeF, Qt, QThreadPool
from PySide6.QtGui import QPageSize, QPainter, QPdfWriter
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from app.ui import label_print, pdf_label_print


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def sheet(tmp_path, app):
    source = tmp_path / "sheet.pdf"
    writer = QPdfWriter(str(source))
    writer.setResolution(72)
    with QPainter(writer) as painter:
        for x, y, mark_size in [(10, 10, 9), (95, 10, 12), (10, 85, 16)]:
            painter.fillRect(x, y, 72, 58, Qt.GlobalColor.white)
            painter.fillRect(x + 8, y + 12, mark_size, mark_size, Qt.GlobalColor.black)
    del writer
    return source


@pytest.fixture
def virtual_printer(tmp_path, monkeypatch):
    target = tmp_path / "printed.pdf"

    class PdfPrinter(QPrinter):
        def __init__(self, mode):
            super().__init__(mode)
            self.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            self.setOutputFileName(str(target))

        def setPrinterName(self, name):
            pass

    monkeypatch.setattr(pdf_label_print, "QPrinter", PdfPrinter)
    monkeypatch.setattr(label_print, "printers", lambda: ["test"])
    return target


def test_print_pdf_keeps_page_count_and_size(tmp_path, app, virtual_printer):
    source = tmp_path / "source.pdf"
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
    job = pdf_label_print.prepare_labels(str(source))
    assert job.unsplit_sheets == 2
    assert [region.page for region in job.regions] == [0, 1]

    try:
        assert pdf_label_print.print_pdf(document, size, "test") == 2
    finally:
        document.close()
    printed, printed_size = pdf_label_print.open_labels(str(virtual_printer))
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


def test_framed_labels_on_one_pdf_page_print_as_separate_labels(sheet, virtual_printer):
    job = pdf_label_print.prepare_labels(str(sheet))
    assert job.unsplit_sheets == 0
    assert [region.page for region in job.regions] == [0, 0, 0]
    first, second, third = (region.bounds for region in job.regions)
    assert first.x() < second.x()
    assert first.y() == second.y() < third.y()
    document, size = pdf_label_print.open_labels(job.data)
    try:
        renderer = pdf_label_print.LabelRenderer(document)
        assert renderer.render(job.regions[0]) != renderer.render(job.regions[-1])
        assert pdf_label_print.print_pdf(document, size, "test", regions=job.regions) == 3
    finally:
        document.close()
    printed, _ = pdf_label_print.open_labels(str(virtual_printer))
    try:
        assert printed.pageCount() == 3
    finally:
        printed.close()


@pytest.mark.parametrize("confirm", [False, True])
def test_page_waits_for_confirmation_before_printing_pdf(sheet, app, monkeypatch, confirm):
    from app.core.settings import AppSettings
    from app.ui import labels_page

    calls = []
    previews = []
    monkeypatch.setattr(label_print, "printers", lambda: ["test"])
    monkeypatch.setattr(labels_page.QFileDialog, "getOpenFileName", lambda *_: (str(sheet), ""))

    def print_pdf(document, _size, printer_name, *, progress, regions):
        calls.append((document.pageCount(), printer_name, len(regions)))
        return len(regions)

    monkeypatch.setattr(pdf_label_print, "print_pdf", print_pdf)

    def confirm_dialog(dialog):
        first = dialog.image.pixmap()
        dialog.next.click()
        previews.append((len(calls), dialog.number.maximum(), dialog.number.value(),
                         dialog.image.pixmap().cacheKey() != first.cacheKey()))
        if confirm:
            dialog.print_button.click()
        else:
            buttons = dialog.findChild(QDialogButtonBox)
            cancel, = [button for button in buttons.buttons()
                       if buttons.buttonRole(button) == QDialogButtonBox.ButtonRole.RejectRole]
            cancel.click()
        return dialog.result()

    monkeypatch.setattr(labels_page.PdfLabelPreview, "exec", confirm_dialog)
    page = labels_page.LabelsPage(AppSettings(), lambda *_: None)
    page.print_pdf_button.click()
    assert QThreadPool.globalInstance().waitForDone(3000)
    app.processEvents()
    assert previews == [(0, 3, 2, True)]
    assert calls == ([(1, "test", 3)] if confirm else [])
    assert page.print_pdf_button.isEnabled()
    page.close()
