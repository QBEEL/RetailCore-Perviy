"""Пометка «сумму поменял человек»: хранилище, импорт 1С и календарь.

Проверяется на локальной базе — правило в ней то же, что на сервере, — и на
виджетах календаря с готовыми оплатами. Сервер не поднимается: его ответ
разбирается из словаря, как пришёл бы по сети.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.payments import Day, Payment, PaymentOrigin, PaymentStatus, service, store

TODAY = date(2026, 7, 30)
HEADER = (
    "Номер;Дата заявки;Есть файлы;Сумма;НДС;Валюта;Статус;Сверх лимита;"
    "Приоритет;Дата платежа;Оплачена / Закрыта;Хозяйственная операция;Получатель;"
    "Состояние ЭДО;Заявитель;Автор"
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "current_user", lambda: "Иванов Евгений")
    return str(tmp_path / "payments.db")


def _csv(tmp_path, amount: str, name: str) -> str:
    row = (f"IP00-000001;09.01.2026;1;{amount};;руб.;К оплате;Нет;;"
           "11.08.2026;Нет;Оплата поставщику;НеваЛайн ООО;;Иванов;Иванов")
    path = tmp_path / name
    path.write_bytes("\n".join([HEADER, row]).encode("cp1251"))
    return str(path)


def _import(tmp_path, db, amount: str, name: str):
    report = service.analyze_import(_csv(tmp_path, amount, name), today=TODAY, db_path=db)
    service.apply_import(report, today=TODAY, db_path=db, link=False)
    return report


def _edit(db, amount: float) -> Payment:
    payment = store.list_payments(None, db)[0]
    payment.amount = amount
    store.save_payment(payment, db)
    return store.list_payments(None, db)[0]


# --- правка суммы ---------------------------------------------------------------------

def test_новая_оплата_без_пометки(db):
    saved = store.save_payment(Payment(amount=100.0, recipient="НеваЛайн ООО"), db)
    assert not store.get_payment(saved.id, db).amount_changed


def test_правка_суммы_ставит_пометку(tmp_path, db):
    _import(tmp_path, db, "100 000,00", "первая.csv")
    changed = _edit(db, 120_000.0)
    assert changed.amount_changed
    assert changed.amount_before == pytest.approx(100_000.0)
    assert changed.amount_changed_by == "Иванов Евгений"
    assert changed.amount_changed_at is not None
    assert changed.amount_change_text.startswith("было 100 000 ₽ · Иванов Евгений")


def test_вторая_правка_помнит_исходную_сумму(tmp_path, db):
    """Важно, от чего ушли, а не предпоследний шаг."""
    _import(tmp_path, db, "100 000,00", "первая.csv")
    _edit(db, 120_000.0)
    again = _edit(db, 130_000.0)
    assert again.amount_before == pytest.approx(100_000.0)


def test_возврат_к_прежней_сумме_снимает_пометку(tmp_path, db):
    _import(tmp_path, db, "100 000,00", "первая.csv")
    _edit(db, 120_000.0)
    back = _edit(db, 100_000.0)
    assert not back.amount_changed
    assert back.amount_before is None and back.amount_changed_by == ""


def test_правка_без_смены_суммы_пометку_не_трогает(tmp_path, db):
    _import(tmp_path, db, "100 000,00", "первая.csv")
    _edit(db, 120_000.0)
    payment = store.list_payments(None, db)[0]
    payment.comment = "уточнить у бухгалтерии"
    store.save_payment(payment, db)
    kept = store.list_payments(None, db)[0]
    assert kept.amount_changed and kept.amount_before == pytest.approx(100_000.0)


# --- импорт 1С ------------------------------------------------------------------------

def test_импорт_не_откатывает_правку_человека(tmp_path, db):
    """В 1С всё та же сумма — правка новее выгрузки и должна пережить импорт."""
    _import(tmp_path, db, "100 000,00", "первая.csv")
    _edit(db, 120_000.0)
    report = _import(tmp_path, db, "100 000,00", "вторая.csv")
    assert report.updated == 0, "предпросмотр не должен обещать изменение"
    kept = store.list_payments(None, db)[0]
    assert kept.amount == pytest.approx(120_000.0)
    assert kept.amount_changed


def test_новая_сумма_из_1с_новее_правки(tmp_path, db):
    _import(tmp_path, db, "100 000,00", "первая.csv")
    _edit(db, 120_000.0)
    _import(tmp_path, db, "150 000,00", "вторая.csv")
    fresh = store.list_payments(None, db)[0]
    assert fresh.amount == pytest.approx(150_000.0)
    assert not fresh.amount_changed and fresh.amount_before is None


def test_1с_догнала_правку_пометка_остаётся(tmp_path, db):
    """Сумму поменяли и в 1С — день всё равно ушёл от исходной суммы."""
    _import(tmp_path, db, "100 000,00", "первая.csv")
    _edit(db, 120_000.0)
    _import(tmp_path, db, "120 000,00", "вторая.csv")
    kept = store.list_payments(None, db)[0]
    assert kept.amount == pytest.approx(120_000.0)
    assert kept.amount_changed


# --- ответ сервера ----------------------------------------------------------------------

def test_пометка_приходит_с_сервера():
    from app.core.payments import remote

    row = {
        "id": 5, "doc_number": "", "request_date": None, "pay_date": "2026-08-11",
        "amount": 120000.0, "vat": 0.0, "currency": "руб.", "supplier_id": 0,
        "recipient": "НеваЛайн ООО", "status": "planned", "source_status": "",
        "paid_flag": False, "operation": "Оплата поставщику", "over_limit": False,
        "priority": "", "edo_state": "", "responsible": "Когай Анна", "author": "",
        "comment": "", "had_files": False, "origin": "manual", "origin_ref": "",
        "created_at": "2026-08-01T10:00:00", "updated_at": "2026-08-02T10:00:00",
        "amount_before": 100000.0, "amount_changed_by": "Когай Анна",
        "amount_changed_at": "2026-08-02T10:00:00",
    }
    payment = remote._payment(row)
    assert payment.amount_changed and payment.amount_changed_by == "Когай Анна"

    for name in ("amount_before", "amount_changed_by", "amount_changed_at"):
        row.pop(name)
    assert not remote._payment(row).amount_changed, "старый сервер — без пометки"


# --- календарь ---------------------------------------------------------------------------

def _changed(amount: float = 120_000.0) -> Payment:
    return Payment(id=1, amount=amount, pay_date=date(2026, 8, 11), recipient="НеваЛайн ООО",
                   status=PaymentStatus.PLANNED, amount_before=100_000.0,
                   amount_changed_by="Когай Анна",
                   amount_changed_at=datetime(2026, 8, 2, 10, 0))


def test_день_считает_поправленные_оплаты():
    day = Day(day=date(2026, 8, 11), payments=[
        _changed(), Payment(amount=5.0, recipient="Бета"), _changed(amount=100_000.0)])
    # Третья — сумма совпала с прежней: пометки нет, хотя поля заполнены.
    assert day.amount_changed == 1


@pytest.fixture(scope="module")
def application():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_клетка_календаря_показывает_точку_и_прежнюю_сумму(application):
    from app.ui.widgets.calendar_grid import DayCell

    cell = DayCell()
    moment = date(2026, 8, 11)
    cell.show_day(moment, Day(day=moment, payments=[_changed()]), today=TODAY)
    assert cell.changed.isVisibleTo(cell)
    assert "сумма изменена вручную: 1" in cell.toolTip()
    assert "было 100 000 ₽ · Когай Анна, 02.08" in cell.toolTip()

    cell.show_day(moment, Day(day=moment, payments=[Payment(amount=5.0, recipient="Бета")]),
                  today=TODAY)
    assert not cell.changed.isVisibleTo(cell)


def test_таблица_и_список_дня_показывают_правку(application, tmp_path, monkeypatch):
    from app.core.payments import data
    from app.core.settings import AppSettings
    from app.ui import payments_page as module

    monkeypatch.setattr(store, "database_path", lambda: str(tmp_path / "page.db"))
    monkeypatch.setattr(data, "online", lambda: False)
    page = module.PaymentsPage(AppSettings(), lambda text, kind: None)
    monkeypatch.setattr(page, "reload", lambda: None)
    page.rows = [_changed(), Payment(id=2, amount=5.0, pay_date=date(2026, 8, 11),
                                     recipient="Бета")]

    page._refresh_table()
    model = page.table.model_
    titles = [column.title for column in model.columns]
    before = titles.index("Было, ₽")
    assert model.index(0, before).data() == "100 000,00 · Когай Анна, 02.08"
    assert model.index(1, before).data() == ""

    page.show_day(Day(day=date(2026, 8, 11), payments=page.rows))
    texts = [page.day_list.item(i).text() for i in range(page.day_list.count())]
    assert "сумма изменена: было 100 000 ₽" in texts[0]
    assert "сумма изменена" not in texts[1]
