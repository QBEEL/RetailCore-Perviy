"""Поле выбора файла и счёт PDF: страница получает книгу Excel, а не PDF."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.ui.widgets.file_picker import EXCEL_FILTER, FilePicker  # noqa: E402
from test_pdf_invoice import _ROWS, _invoice  # noqa: E402


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def warnings(monkeypatch) -> list[str]:
    """Окно предупреждения в тесте ждало бы нажатия — вместо него список."""
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda parent, title, text, *rest: shown.append(text))
    return shown


def _picked(picker: FilePicker) -> list[str]:
    selected: list[str] = []
    picker.file_selected.connect(selected.append)
    return selected


def test_pdf_becomes_workbook_next_to_it(application, warnings, tmp_path: Path) -> None:
    picker = FilePicker("Счёт")
    selected = _picked(picker)

    picker.set_path(str(_invoice(tmp_path, _ROWS)))

    workbook_path = str(tmp_path / "счёт.xlsx")
    assert selected == [workbook_path]
    assert picker.path == workbook_path
    assert os.path.exists(workbook_path)
    assert warnings == []


def test_newer_workbook_is_not_overwritten(application, warnings, tmp_path: Path) -> None:
    """Книгу после перевода могли поправить руками — она важнее PDF."""
    pdf = _invoice(tmp_path, _ROWS)
    edited = tmp_path / "счёт.xlsx"
    edited.write_bytes(b"edited by hand")
    os.utime(edited, (os.path.getmtime(pdf) + 60,) * 2)

    picker = FilePicker("Счёт")
    selected = _picked(picker)
    picker.set_path(str(pdf))

    assert selected == [str(edited)]
    assert edited.read_bytes() == b"edited by hand"


def test_unreadable_pdf_is_not_selected(application, warnings, tmp_path: Path) -> None:
    broken = tmp_path / "битый.pdf"
    broken.write_bytes(b"not a pdf")
    picker = FilePicker("Счёт")
    selected = _picked(picker)

    picker.set_path(str(broken))

    assert selected == []
    assert picker.path == ""
    assert len(warnings) == 1


def test_excel_only_picker_leaves_pdf_alone(application, warnings, tmp_path: Path) -> None:
    """Поле, где PDF не ждут (файл плана), не переводит его и не создаёт книгу."""
    pdf = _invoice(tmp_path, _ROWS)
    picker = FilePicker("План", file_filter=EXCEL_FILTER)
    picker.set_path(str(pdf))
    assert not (tmp_path / "счёт.xlsx").exists()


# --- «Скачать Excel» -------------------------------------------------------------

def test_download_button_only_for_pdf(application, warnings, tmp_path: Path) -> None:
    picker = FilePicker("Счёт")
    picker.set_path(str(_invoice(tmp_path, _ROWS)))
    assert not picker.download_button.isHidden()

    other = tmp_path / "прайс.xlsx"
    other.write_bytes((tmp_path / "счёт.xlsx").read_bytes())
    picker.set_path(str(other))
    assert picker.download_button.isHidden()


def test_download_saves_copy_where_chosen(application, warnings, monkeypatch,
                                          tmp_path: Path) -> None:
    from PySide6.QtWidgets import QFileDialog

    picker = FilePicker("Счёт")
    picker.set_path(str(_invoice(tmp_path, _ROWS)))
    chosen = tmp_path / "загрузки" / "мой счёт"
    chosen.parent.mkdir()
    offered: list[str] = []

    def save_dialog(parent, caption, suggested, file_filter):
        offered.append(suggested)
        return str(chosen), file_filter

    monkeypatch.setattr(QFileDialog, "getSaveFileName", save_dialog)
    picker.download_excel()

    saved = chosen.with_name("мой счёт.xlsx")
    assert saved.read_bytes() == (tmp_path / "счёт.xlsx").read_bytes()
    assert offered[0].endswith("счёт.xlsx")
    assert warnings == []


def test_download_cancelled_writes_nothing(application, warnings, monkeypatch,
                                           tmp_path: Path) -> None:
    from PySide6.QtWidgets import QFileDialog

    picker = FilePicker("Счёт")
    picker.set_path(str(_invoice(tmp_path, _ROWS)))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    before = sorted(p.name for p in tmp_path.iterdir())

    picker.download_excel()

    assert sorted(p.name for p in tmp_path.iterdir()) == before
