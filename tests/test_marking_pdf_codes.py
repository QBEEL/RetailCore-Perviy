"""Импорт текстовых кодов из PDF ЧЗ без рабочих документов."""
from __future__ import annotations

import pytest

from app.core.marking import pdf_codes


GTIN = "04610600250062"


def test_pdf_codes_accepts_only_valid_code_lines(monkeypatch):
    codes = [f"01{GTIN}21SERIAL{i:06d}" for i in range(3)]
    monkeypatch.setattr(pdf_codes, "extract_text", lambda _path: (
        "Заказ · 3 кода\n" + "\n".join(codes) + "\nGTIN 04610600250062\n"))

    assert pdf_codes.read("synthetic.pdf") == codes


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
