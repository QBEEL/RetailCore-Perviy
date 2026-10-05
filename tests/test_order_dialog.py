"""Окно «Новый заказ кодов»: форма, вставка списка, подсказки и шаблоны.

Окно ничего не отправляет — оно собирает `Request`, поэтому проверяется то, что
оно собрало и что сказало человеку до отправки.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.core.marking import orders as orders_module
from app.core.marking.orders import Buffer, Order, ReleaseMethod, parse_lines
from app.ui.widgets import order_dialog
from app.ui.widgets.order_dialog import OrderDialog


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def _filled(application, orders=(), certificate=True) -> OrderDialog:
    dialog = OrderDialog(list(orders), certificate, "Иванов Евгений")
    dialog.add_line("04601234567893", 500)
    return dialog


def _past(template: int, group: str = "lp") -> Order:
    order = Order(id="з-1", status="READY", product_group=group,
                  created_at=datetime(2026, 8, 3), raw={"paymentType": 2})
    order.buffers = [Buffer(gtin="046", raw={"templateId": template})]
    return order


# --- разбор вставленного списка ------------------------------------------------------

def test_список_из_excel_разбирается_по_табуляции():
    found = parse_lines("04601234567893\t500\n04607177964080\t25\n")

    assert found.lines == [("04601234567893", 500), ("04607177964080", 25)]
    assert not found.skipped


def test_штрихкод_из_13_цифр_дополняется_до_gtin14():
    assert parse_lines("4601234567893 12").lines == [("04601234567893", 12)]


def test_одинаковые_коды_складываются():
    found = parse_lines("04601234567893;100\n04601234567893;50")

    assert found.lines == [("04601234567893", 150)]
    assert found.merged == 1


def test_заголовок_и_мусор_пропускаются_а_не_ломают_вставку():
    found = parse_lines("GTIN\tКоличество\n04601234567893\t5\nчто-то\n04600000000007\t0")

    assert found.lines == [("04601234567893", 5)]
    assert len(found.skipped) == 3


def test_количество_с_пробелом_разрядов_читается():
    assert parse_lines('04601234567893\t1 000').lines == [("04601234567893", 1000)]


# --- форма ----------------------------------------------------------------------------

def test_таблица_и_редактор_ячейки_крупные(application):
    """Раньше цифры при правке были мелкие — это и было причиной окна."""
    dialog = _filled(application)

    assert dialog.lines.font().pointSize() >= 13
    assert dialog.lines.verticalHeader().defaultSectionSize() >= 36
    editor = dialog.lines.itemDelegateForColumn(0).createEditor(
        dialog.lines, None, dialog.lines.model().index(0, 0))
    assert editor.font().pointSize() > dialog.lines.font().pointSize() - 1


def test_окно_вмещает_много_товаров_и_собирает_их_все(application):
    dialog = OrderDialog([], True, "Иванов")
    for number in range(60):
        dialog.add_line(f"0460000000{number:04d}", 10 + number)

    request = dialog.request

    assert len(request.lines) == 60
    assert request.total == sum(10 + number for number in range(60))
    assert dialog.lines.rowCount() == 60


def test_вставка_из_буфера_добавляет_строки(application):
    dialog = OrderDialog([], True, "Иванов")
    QApplication.clipboard().setText("4601234567893\t100\n04607177964080\t250")

    dialog.paste_lines()

    assert [(line.gtin, line.quantity) for line in dialog.request.lines] == [
        ("04601234567893", 100), ("04607177964080", 250)]
    assert "Добавлено строк: 2" in dialog.hint.text()


def test_вставка_товара_который_уже_есть_прибавляет_количество(application):
    dialog = _filled(application)
    QApplication.clipboard().setText("04601234567893\t200")

    dialog.paste_lines()

    assert [(line.gtin, line.quantity) for line in dialog.request.lines] == [
        ("04601234567893", 700)]


def test_пустой_буфер_называет_причину(application):
    dialog = OrderDialog([], True, "Иванов")
    QApplication.clipboard().setText("просто текст")

    dialog.paste_lines()

    assert dialog.lines.rowCount() == 0
    assert "нет строк" in dialog.hint.text()


def test_неразобранные_строки_видны_а_не_теряются_молча(application):
    dialog = OrderDialog([], True, "Иванов")
    QApplication.clipboard().setText("04601234567893\t5\nмусор")

    dialog.paste_lines()

    assert dialog.lines.rowCount() == 1
    assert "не разобрано строк: 1" in dialog.hint.text()


def test_удаляются_все_выделенные_строки(application):
    dialog = OrderDialog([], True, "Иванов")
    for number in range(4):
        dialog.add_line(f"0460000000{number:04d}", 1)
    dialog.lines.selectRow(1)
    dialog.lines.selectionModel().select(
        dialog.lines.model().index(2, 0),
        dialog.lines.selectionModel().SelectionFlag.Select
        | dialog.lines.selectionModel().SelectionFlag.Rows)

    dialog.remove_line()

    assert dialog.lines.rowCount() == 2


# --- что говорит подсказка --------------------------------------------------------------

def test_одинаковые_товары_называются_до_отправки(application):
    dialog = _filled(application)
    dialog.add_line("04601234567893", 10)

    assert not dialog.order_button.isEnabled()
    assert "одинаковые коды товаров" in dialog.hint.text().lower()


def test_без_сертификата_заказ_отправить_нельзя(application):
    dialog = _filled(application, certificate=False)

    assert not dialog.order_button.isEnabled()
    assert "не выбран сертификат" in dialog.hint.text()


def test_готовый_заказ_включает_кнопку_и_считает_числа(application):
    dialog = _filled(application)

    assert dialog.order_button.isEnabled()
    assert "1 товар," in dialog.hint.text()
    assert "500 кодов" in dialog.hint.text()


def test_производство_названо_неготовым_на_экране(application):
    dialog = _filled(application)
    dialog.method_box.setCurrentIndex(
        dialog.method_box.findData(ReleaseMethod.PRODUCTION.value))

    assert not dialog.order_button.isEnabled()
    assert "производственной площадке" in dialog.hint.text()


def test_контактное_лицо_подставлено_и_обязательно(application):
    dialog = OrderDialog([], True, "")
    dialog.add_line("04601234567893", 5)

    assert "контактное лицо" in dialog.hint.text()
    dialog.contact_edit.setText("Иванов")
    assert dialog.order_button.isEnabled()


def test_кнопка_заказа_не_кнопка_по_умолчанию(application):
    """Enter при правке ячейки не должен отправлять заказ."""
    dialog = _filled(application)

    assert not dialog.order_button.autoDefault()
    assert not dialog.order_button.isDefault()


# --- шаблоны ----------------------------------------------------------------------------

def test_шаблон_подставляется_из_прошлого_заказа_и_объясняется(application):
    """Число «10» само по себе не значит ничего — важно, откуда оно взялось."""
    dialog = OrderDialog([_past(14)], True, "Иванов")

    dialog.group_box.setCurrentIndex(dialog.group_box.findData("lp"))

    # Справочник для «lp» знает шаблон 10, но СУЗ однажды приняла 14 —
    # реальность старше документа, и выбран именно он.
    assert dialog.template_box.currentData() == 14
    assert dialog.payment_box.value() == 2
    assert "03.08.2026" in dialog.template_hint.text()
    assert "шаблон" not in " ".join(
        dialog._problems()).lower().replace("не выбран шаблон", "")


def test_группа_без_истории_берёт_шаблон_из_справочника(application):
    """Именно на этом и споткнулся первый заказ: духам подставлялся шаблон «lp»."""
    dialog = OrderDialog([_past(10)], True, "Иванов")

    dialog.group_box.setCurrentIndex(dialog.group_box.findData("perfumery"))

    assert dialog.template_box.currentData() == 9
    assert "справочника СУЗ" in dialog.template_hint.text()
    # Шаблоны различаются длиной серийного номера и криптохвостом — это и
    # написано в строке.
    assert "серийный номер 13 знаков" in dialog.template_box.currentText()


def test_шаблон_идёт_в_каждую_строку_заказа(application):
    dialog = OrderDialog([], True, "Иванов")
    dialog.group_box.setCurrentIndex(dialog.group_box.findData("perfumery"))
    dialog.add_line("04601234567893", 5)
    dialog.add_line("04607177964080", 7)

    assert {line.template_id for line in dialog.request.lines} == {9}
    assert dialog.request.product_group == "perfumery"
    assert orders_module.template_fits("perfumery", 9)


def test_принятие_без_ошибок_закрывает_окно(application):
    dialog = _filled(application)

    dialog._accept_order()

    assert dialog.result() == dialog.DialogCode.Accepted


def test_принятие_с_ошибками_окно_не_закрывает(application):
    dialog = OrderDialog([], True, "Иванов")

    dialog._accept_order()

    assert dialog.result() != dialog.DialogCode.Accepted
    assert order_dialog._plural(2, "товар", "товара", "товаров") == "2 товара"
