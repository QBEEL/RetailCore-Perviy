"""Диалоги получения, печати и ввода в оборот — настоящие, без подмены.

Принтера в тестах нет, поэтому печать идёт в PDF: это тот же `print_codes` и тот
же макет, отличается только получатель. Выбор файла подменяется ответом
«сохранить сюда».
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog

from app.core.marking import issue, labels
from app.core.marking.codes import GS
from app.core.marking.models import Contour
from app.core.marking.orders import Buffer, Order
from app.ui import label_print
from app.ui.widgets import issue_dialogs
from app.ui.widgets.issue_dialogs import IntroduceDialog, IssueDialog, PrintDialog

GTIN = "04607177964080"
CODES = [f"01{GTIN}21Serial{number:04d}{GS}91EE11{GS}92dGVzdGRhdGFmb3JsYWJlbA=="
         for number in range(5)]


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def batch():
    block = issue.Batch(id="b1", gtin=GTIN, name="Колготки женские", tnved="6115950000",
                        codes=list(CODES), created="2026-10-05T10:00:00")
    issue.save(block)
    return block


def _save_to(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *args, **kwargs: (str(path), "")))


def test_печать_в_pdf_ставит_отметку_и_пишет_страницу_на_этикетку(
        application, batch, tmp_path, monkeypatch):
    target = tmp_path / "labels.pdf"
    _save_to(monkeypatch, target)
    dialog = PrintDialog(batch)
    dialog.pdf_box.setChecked(True)
    dialog.count.setValue(3)

    dialog._print()

    assert target.exists() and target.stat().st_size > 0
    assert dialog.printed_now == 3
    assert issue.load("b1").printed == 3
    # Следующая печать продолжит с четвёртой этикетки.
    assert dialog.start.value() == 4


def test_допечатка_начинается_с_первой_ненапечатанной(application, batch):
    batch.printed = 2
    issue.save(batch)

    dialog = PrintDialog(batch)

    assert dialog.start.value() == 3
    assert dialog.count.value() == 3
    assert dialog._selected() == CODES[2:]


def test_печать_без_принтера_и_без_pdf_недоступна(application, batch, monkeypatch):
    monkeypatch.setattr(label_print, "printers", lambda: [])
    monkeypatch.setattr(label_print, "default_printer", lambda: "")

    dialog = PrintDialog(batch)

    assert not dialog.print_button.isEnabled()
    assert "не найден ни один принтер" in dialog.hint.text()
    dialog.pdf_box.setChecked(True)
    assert dialog.print_button.isEnabled()


def test_слишком_большой_символ_блокирует_печать(application, batch):
    dialog = PrintDialog(batch)
    dialog.dm_size.setValue(40)

    assert not dialog.print_button.isEnabled()
    assert "не помещается" in dialog.hint.text()


def test_настройки_макета_запоминаются(application, batch, tmp_path, monkeypatch):
    _save_to(monkeypatch, tmp_path / "x.pdf")
    dialog = PrintDialog(batch)
    dialog.pdf_box.setChecked(True)
    dialog.width.setValue(58)
    dialog.height.setValue(40)
    dialog._print()

    saved = labels.load().spec()

    assert (saved.width, saved.height) == (58, 40)
    assert PrintDialog(batch).width.value() == 58


def test_пробная_этикетка_не_расходует_коды(application, batch, tmp_path, monkeypatch):
    _save_to(monkeypatch, tmp_path / "test.pdf")
    dialog = PrintDialog(batch)
    dialog.pdf_box.setChecked(True)

    dialog._print_test()

    assert (tmp_path / "test.pdf").exists()
    assert issue.load("b1").printed == 0


def _intro(batch, inn="250101802801", problem="", contour=Contour.SANDBOX):
    return IntroduceDialog(batch, inn, contour, problem)


def test_ввод_в_оборот_по_умолчанию_берёт_напечатанные_и_не_отправленные(application,
                                                                         batch):
    batch.printed = 4
    batch.introduced_count = 1
    issue.save(batch)

    dialog = _intro(batch)

    assert dialog.scope_box.currentData() == "pending"
    assert dialog._codes() == CODES[1:4]


def test_ввод_в_оборот_без_напечатанных_предлагает_весь_блок(application, batch):
    dialog = _intro(batch)

    assert dialog.scope_box.currentData() == "all"
    assert len(dialog._codes()) == 5


def test_всё_напечатанное_уже_отправлено_предлагает_все_напечатанные(application, batch):
    batch.printed = 3
    batch.introduced_count = 3
    issue.save(batch)

    dialog = _intro(batch)

    assert dialog.scope_box.currentData() == "printed"
    assert len(dialog._codes()) == 3


def test_файл_ввода_в_оборот_сохраняется_и_отмечается(application, batch, tmp_path,
                                                      monkeypatch):
    target = tmp_path / "vvod.xml"
    _save_to(monkeypatch, target)
    dialog = _intro(batch)

    dialog._save_file()

    text = target.read_text(encoding="utf-8")
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert text.count("<product>") == 5
    assert dialog.saved_path == str(target) and dialog.action == "file"
    assert issue.load("b1").introduced
    # Файл — не отправка: номера документа нет.
    assert issue.load("b1").doc_id == ""


def test_без_инн_ни_файл_ни_отправка_не_доступны(application, batch, tmp_path,
                                                 monkeypatch):
    target = tmp_path / "vvod.xml"
    _save_to(monkeypatch, target)
    dialog = _intro(batch, inn="")

    assert not dialog.file_button.isEnabled()
    assert not dialog.send_button.isEnabled()
    assert "ИНН" in dialog.hint.text()
    dialog._save_file()
    assert not target.exists()


def test_отправка_недоступна_с_причиной_но_файл_сохранить_можно(application, batch):
    dialog = _intro(batch, problem="не выполнен вход по сертификату.")

    assert not dialog.send_button.isEnabled()
    assert dialog.file_button.isEnabled()
    assert "не выполнен вход" in dialog.hint.text()


def test_отправка_спрашивает_подтверждение_и_без_него_ничего_не_делает(
        application, batch, monkeypatch):
    dialog = _intro(batch)
    monkeypatch.setattr(dialog, "_confirm_send", lambda document: False)

    dialog._send()

    assert dialog.action == "" and dialog.result() != dialog.DialogCode.Accepted


def test_подтверждённая_отправка_запоминает_сколько_кодов_покрыто(application, batch,
                                                                 monkeypatch):
    batch.printed = 3
    issue.save(batch)
    dialog = _intro(batch)
    monkeypatch.setattr(dialog, "_confirm_send", lambda document: True)

    dialog._send()

    assert dialog.action == "send"
    assert dialog.covered == 3
    assert len(dialog.document.codes) == 3


def test_кнопки_отправки_не_кнопки_по_умолчанию(application, batch):
    """Enter, нажатый по привычке, ничего не отправляет."""
    dialog = _intro(batch)

    assert not dialog.send_button.autoDefault() and not dialog.send_button.isDefault()
    assert not dialog.file_button.autoDefault()


def test_прежняя_отправка_видна_в_окне(application, batch):
    batch.doc_id = "doc-777"
    batch.doc_status = "CHECKED_OK"
    batch.introduced_count = 2
    issue.save(batch)

    dialog = _intro(batch)

    assert "doc-777" in dialog.history.text()


def test_получение_предлагает_весь_остаток_и_подставляет_название(application):
    order = Order(id="o-1", buffers=[
        Buffer(gtin=GTIN, left=250, total=1000, passed=750),
        Buffer(gtin="04600000000007", left=0, total=10, passed=10)])
    products = {GTIN: issue.Product(gtin=GTIN, name="Колготки", tnved="6115950000")}

    dialog = IssueDialog(order, products, Contour.SANDBOX)

    # Исчерпанный буфер в выбор не попадает.
    assert dialog.buffer_box.count() == 1
    assert dialog.gtin == GTIN
    assert dialog.count == 250
    assert dialog.product.name == "Колготки" and dialog.product.tnved == "6115950000"


def test_получение_без_остатка_кнопки_не_даёт(application):
    order = Order(id="o-1", buffers=[Buffer(gtin=GTIN, left=0)])

    dialog = IssueDialog(order, {}, Contour.SANDBOX)

    assert not dialog.confirm_box.button(
        issue_dialogs.QDialogButtonBox.StandardButton.Ok).isEnabled()


# --- окно ввода в оборот: перемаркировка -------------------------------------------------

def test_по_умолчанию_предлагается_перемаркировка_с_причиной_испорчено(application,
                                                                       batch):
    dialog = _intro(batch)

    assert dialog.kind == "remark"
    assert dialog.cause_box.currentData() == "KM_SPOILED"
    assert not dialog.remark_box.isHidden()


def test_документ_перемаркировки_собирается_из_полей_окна(application, batch):
    batch.printed = 5
    issue.save(batch)
    dialog = _intro(batch)
    dialog.country_edit.setText("156")
    dialog.color_edit.setText("чёрный")
    dialog.size_edit.setText("M")

    document = dialog.document

    assert document.kind == "remark"
    assert (document.cause, document.country, document.color, document.size) == (
        "KM_SPOILED", "156", "чёрный", "M")
    assert document.date == dialog.date_edit.date().toString("yyyy-MM-dd")
    assert len(document.codes) == 5 and document.previous == ()
    assert document.problems == []


def test_предыдущие_коды_читаются_по_строкам_и_сверяются_по_числу(application, batch):
    dialog = _intro(batch)
    dialog.previous_edit.setPlainText("\n  01046071779640802 1Old1  \n\n")

    assert len(dialog.document.previous) == 1
    assert "поровну" in dialog.hint.text()
    assert not dialog.send_button.isEnabled()


def test_переключение_на_остатки_прячет_поля_перемаркировки(application, batch):
    dialog = _intro(batch)

    dialog.kind_box.setCurrentIndex(dialog.kind_box.findData("ostatky"))

    assert dialog.remark_box.isHidden()
    assert dialog.document.kind == "ostatky"


def test_причина_отправки_разная_у_двух_видов_документа(application, batch):
    dialog = IntroduceDialog(batch, "250101802801", Contour.SANDBOX,
                             {"remark": "", "ostatky": "группа не подключена."})

    assert dialog.send_button.isEnabled()
    dialog.kind_box.setCurrentIndex(dialog.kind_box.findData("ostatky"))
    assert not dialog.send_button.isEnabled()
    assert "группа не подключена" in dialog.hint.text()


def test_файл_перемаркировки_сохраняется_json_и_запоминает_сведения(
        application, batch, tmp_path, monkeypatch):
    import json as json_module

    target = tmp_path / "remark.json"
    _save_to(monkeypatch, target)
    dialog = _intro(batch)
    dialog.color_edit.setText("чёрный")

    dialog._save_file()

    body = json_module.loads(target.read_text(encoding="utf-8"))
    assert body["remarking_cause"] == "KM_SPOILED"
    assert len(body["products"]) == 5
    saved = issue.load("b1")
    assert saved.color == "чёрный" and saved.introduced
    assert labels.load().introduce_kind == "remark"


def test_файл_перемаркировки_можно_сохранить_и_xml(application, batch, tmp_path,
                                                  monkeypatch):
    target = tmp_path / "remark.xml"
    _save_to(monkeypatch, target)
    dialog = _intro(batch)

    dialog._save_file()

    assert target.read_text(encoding="utf-8").lstrip().startswith(
        '<?xml version="1.0" encoding="UTF-8"?>\n<remark version="7">')


def test_выбранный_вид_и_причина_запоминаются_для_следующего_раза(application, batch,
                                                                monkeypatch):
    dialog = _intro(batch)
    dialog.cause_box.setCurrentIndex(dialog.cause_box.findData("RETAIL_RETURN"))
    monkeypatch.setattr(dialog, "_confirm_send", lambda document: True)

    dialog._send()

    reopened = _intro(batch)
    assert reopened.cause_box.currentData() == "RETAIL_RETURN"
    assert reopened.kind == "remark"


def test_подтверждение_называет_документ_и_причину(application, batch, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    shown: list = []

    def fake_exec(self):
        shown.append(self.text())
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    dialog = _intro(batch)

    dialog._confirm_send(dialog.document)

    assert "Перемаркировка" in shown[0] and "KM_SPOILED" in shown[0]
    assert "250101802801" in shown[0]


def test_сведения_о_товаре_подставляются_из_блока(application, batch):
    batch.country, batch.color, batch.size = "643", "белый", "L"
    issue.save(batch)

    dialog = _intro(batch)

    assert (dialog.country_edit.text(), dialog.color_edit.text(),
            dialog.size_edit.text()) == ("643", "белый", "L")
