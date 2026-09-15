"""Вкладка «Оплаты»: перенос оплат на другой день перетаскиванием.

База временная, окно строится по-настоящему. Мышь не двигается — проверяется
то, что делает страница, когда перенос до неё дошёл: какие оплаты уходят в
базу, что происходит с оплаченными строками в выделении и о чём спрашивают
перед переносом дня целиком.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication, QMessageBox

from app.core.payments import Day, Payment, PaymentStatus, data, store
from app.core.settings import AppSettings
from app.ui.widgets.calendar_grid import DayCell, payment_ids, payment_mime

DAY = date(2026, 9, 10)
TARGET = date(2026, 9, 17)


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(application, monkeypatch, tmp_path):
    """Страница оплат на временной базе, без общей и без фоновой перезагрузки."""
    from app.ui import payments_page as module

    database = str(tmp_path / "payments.db")
    monkeypatch.setattr(store, "database_path", lambda: database)
    monkeypatch.setattr(data, "online", lambda: False)

    for amount, status, name in [
        (10000.0, PaymentStatus.PLANNED, "Альфа"),
        (20000.0, PaymentStatus.PLANNED, "Бета"),
        (30000.0, PaymentStatus.OVERDUE, "Гамма"),
        (40000.0, PaymentStatus.PAID, "Дельта"),
    ]:
        store.save_payment(Payment(amount=amount, pay_date=DAY, status=status, recipient=name))

    toasts: list[tuple[str, object]] = []
    widget = module.PaymentsPage(AppSettings(), lambda text, kind: toasts.append((text, kind)))
    widget.toasts = toasts
    widget.rows = store.list_payments()
    # Перезагрузка уходит в поток и тянет за собой общую базу: для переноса
    # важно, что легло в базу, а не как страница себя перерисовала.
    monkeypatch.setattr(widget, "reload", lambda: None)
    return widget


def rows_by_name(name: str) -> Payment:
    return next(p for p in store.list_payments() if p.recipient == name)


def ids_of(page, *names: str) -> list[int]:
    return [p.id for p in page.rows if p.recipient in names]


def test_перенос_выбранных_оплат_меняет_дату_и_статус(page):
    page.move_payments(ids_of(page, "Альфа", "Гамма"), TARGET)

    for name in ("Альфа", "Гамма"):
        moved = rows_by_name(name)
        assert moved.pay_date == TARGET
        assert moved.status is PaymentStatus.MOVED
    # Не выбранное остаётся на месте — переносится ровно выделение.
    assert rows_by_name("Бета").pay_date == DAY
    assert page.toasts[-1][0].startswith("2 оплаты: перенесено на 17.09.2026")


def test_оплаченное_из_выделения_пропускается(page):
    page.move_payments(ids_of(page, "Альфа", "Дельта"), TARGET)

    assert rows_by_name("Альфа").pay_date == TARGET
    paid = rows_by_name("Дельта")
    assert paid.pay_date == DAY
    assert paid.status is PaymentStatus.PAID


def test_переносить_нечего_если_выбрано_только_оплаченное(page):
    page.move_payments(ids_of(page, "Дельта"), TARGET)

    assert rows_by_name("Дельта").pay_date == DAY
    assert "Переносить нечего" in page.toasts[-1][0]


def test_перенос_на_тот_же_день_ничего_не_меняет(page):
    page.move_payments(ids_of(page, "Альфа"), DAY)

    assert rows_by_name("Альфа").status is PaymentStatus.PLANNED


def test_день_целиком_переносится_после_подтверждения(page, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    page.move_payments(ids_of(page, "Альфа", "Бета", "Гамма"), TARGET, True)

    assert {rows_by_name(n).pay_date for n in ("Альфа", "Бета", "Гамма")} == {TARGET}


def test_отказ_от_переноса_дня_ничего_не_трогает(page, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    page.move_payments(ids_of(page, "Альфа", "Бета", "Гамма"), TARGET, True)

    assert {rows_by_name(n).pay_date for n in ("Альфа", "Бета", "Гамма")} == {DAY}


def test_одна_оплата_за_клетку_переносится_без_вопроса(page, monkeypatch):
    """Подтверждают перенос дня, а не единственной оплаты в нём."""
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: pytest.fail("лишний вопрос")))
    page.move_payments(ids_of(page, "Альфа"), TARGET, True)

    assert rows_by_name("Альфа").pay_date == TARGET


def test_список_дня_отдаёт_в_перенос_только_неоплаченное(page):
    day = Day(day=DAY, payments=list(page.rows))
    page.show_day(day)
    page.day_list.selectAll()

    chosen = page.day_list.selected_ids()
    assert set(chosen) == set(ids_of(page, "Альфа", "Бета", "Гамма"))
    assert rows_by_name("Дельта").id not in chosen


def test_выделение_из_одной_строки_переносит_её_одну(page):
    page.show_day(Day(day=DAY, payments=list(page.rows)))
    row = next(i for i in range(page.day_list.count())
               if page.day_list.item(i).text().find("Бета") >= 0)
    page.day_list.setCurrentRow(row, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    page.move_payments(page.day_list.selected_ids(), TARGET)

    assert rows_by_name("Бета").pay_date == TARGET
    assert rows_by_name("Альфа").pay_date == DAY


def test_клетка_дня_отдаёт_идентификаторы_переноса(page):
    cell = DayCell()
    cell.show_day(DAY, Day(day=DAY, payments=list(page.rows)))
    seen: list[tuple] = []
    cell.payments_dropped.connect(lambda ids, day, whole: seen.append((ids, day, whole)))

    # Приход переноса — то же, что делает Qt при отпускании кнопки над клеткой.
    cell.payments_dropped.emit(payment_ids(payment_mime(ids_of(page, "Альфа"))), TARGET, False)

    assert seen == [([rows_by_name("Альфа").id], TARGET, False)]
