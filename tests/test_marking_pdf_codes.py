"""Импорт текстовых кодов из PDF ЧЗ без рабочих документов."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPdfWriter
from PySide6.QtWidgets import QApplication

from app.core.marking import codes, datamatrix, pdf_codes


GTIN = "04610600250062"


def test_pdf_codes_accepts_only_valid_code_lines(monkeypatch):
    codes = [f"01{GTIN}21SERIAL{i:06d}" for i in range(3)]
    monkeypatch.setattr(pdf_codes, "extract_text", lambda _path: (
        "Заказ · 3 кода\n" + "\n".join(codes) + "\nGTIN 04610600250062\n"))

    assert pdf_codes.read("synthetic.pdf") == codes


def test_pdf_codes_restores_wrapped_serials_with_punctuation(monkeypatch):
    first = f'01{GTIN}21ABC;,"DEFG123'
    second = f"01{GTIN}211234567890123"
    monkeypatch.setattr(pdf_codes, "extract_text", lambda _path: (
        f"{first[:24]}\n{first[24:]}\n\n{second[:25]}\n{second[25:]}\n\n\f"
    ))
    assert pdf_codes.read("synthetic.pdf") == [first, second]


def test_pdf_codes_does_not_join_across_page_breaks(monkeypatch):
    first = f"01{GTIN}21ABC123"
    monkeypatch.setattr(pdf_codes, "extract_text", lambda _path: f"{first}\n\fEFGH456\n")
    assert pdf_codes.read("synthetic.pdf") == [first]


def test_pdf_codes_rejects_image_only_pdf(monkeypatch):
    monkeypatch.setattr(pdf_codes, "extract_text", lambda _path: "Заказ · 3 кода")

    with pytest.raises(pdf_codes.PdfCodesProblem, match="не найдено текстовых кодов"):
        pdf_codes.read("synthetic.pdf")


def test_pdf_codes_reports_broken_pdf(monkeypatch):
    def broken(_path):
        raise ValueError("broken")

    monkeypatch.setattr(pdf_codes, "extract_text", broken)
    with pytest.raises(pdf_codes.PdfCodesProblem, match="Не удалось прочитать PDF"):
        pdf_codes.read("synthetic.pdf")


def test_full_marks_are_decoded_from_data_matrix_on_multiple_pdf_pages(tmp_path, monkeypatch):
    application = QApplication.instance() or QApplication([])
    path = tmp_path / "labels.pdf"
    identifiers = [f"01{GTIN}21SERIAL{i:07d}" for i in range(2)]
    marks = [f"{ki}{codes.GS}91TEST{codes.GS}92{'A' * 44}" for ki in identifiers]
    writer = QPdfWriter(str(path))
    writer.setResolution(72)
    painter = QPainter(writer)
    try:
        for page, raw in enumerate(marks):
            if page:
                assert writer.newPage()
            for y, row in enumerate(datamatrix.encode(raw)):
                for x, dark in enumerate(row):
                    if dark:
                        painter.fillRect(36 + x * 3, 36 + y * 3, 3, 3, Qt.GlobalColor.black)
    finally:
        painter.end()

    assert pdf_codes.read_full(str(path)) == marks
    monkeypatch.setattr(pdf_codes, "read", lambda _path: identifiers + [identifiers[0]] * 2)
    assert pdf_codes.scan_full(str(path)) == pdf_codes.FullScan(marks, 2, 0)
    with pytest.raises(pdf_codes.PdfCodesProblem, match="для 2 кодов"):
        pdf_codes.read_full(str(path))
    application.processEvents()


def test_full_pdf_import_rejects_text_without_crypto(tmp_path):
    application = QApplication.instance() or QApplication([])
    path = tmp_path / "text.pdf"
    writer = QPdfWriter(str(path))
    painter = QPainter(writer)
    try:
        painter.drawText(100, 300, f"01{GTIN}21SERIAL1234567")
    finally:
        painter.end()

    with pytest.raises(pdf_codes.PdfCodesProblem, match="не найдено марок с криптохвостом"):
        pdf_codes.read_full(str(path))
    application.processEvents()
