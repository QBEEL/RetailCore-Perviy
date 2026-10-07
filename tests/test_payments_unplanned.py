"""«Не было запланировано»: заявка из 1С, на которую в приложении не было плана.

Проверяется на настоящем импорте во временной базе: что получает пометку, что
нет, и что она переживает повторные выгрузки.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.payments import Day, Payment, PaymentOrigin, importer, service, store

TODAY = date(2026, 10, 7)

HEADER = (
    "Номер;Дата заявки;Есть файлы;Сумма;НДС;Валюта;Статус;Сверх лимита;Приоритет;"
    "Дата платежа;Оплачена / Закрыта;Хозяйственная операция;Получатель;"
    "Состояние ЭДО;Заявитель;Автор"
)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "payments.db")


def _row(number: str, amount: str, recipient: str, pay: str = "20.10.2026",
         operation: str = "Оплата поставщику") -> str:
    return (f"{number};01.10.2026;1;{amount};;руб.;К оплате;Нет;;"
            f"{pay};Нет;{operation};{recipient};;Иванов;Иванов")


def _file(tmp_path, name: str, *rows: str) -> str:
    path = tmp_path / name
    path.write_bytes("\n".join([HEADER, *rows]).encode("cp1251"))
    return str(path)


def _import(path: str, db: str):
    report = service.analyze_import(path, today=TODAY, db_path=db)
    service.apply_import(report, today=TODAY, db_path=db, link=False)
    return report


def _by_doc(db: str) -> dict[str, Payment]:
    return {p.doc_number: p for p in store.list_payments(path=db)}


def test_первая_выгрузка_ничего_не_помечает(tmp_path, db):
    """Вся история приходит разом — «не планировалось» было бы у каждой заявки."""
    report = service.analyze_import(
        _file(tmp_path, "первая.csv", _row("IP00-1", "100 000,00", "Альфа ООО"),
              _row("IP00-2", "200 000,00", "Бета ООО")), today=TODAY, db_path=db)

    assert report.unplanned == 0
    assert not any(row.unplanned for row in report.details)


def test_новая_заявка_после_прошлой_выгрузки_не_была_запланирована(tmp_path, db):
    _import(_file(tmp_path, "первая.csv", _row("IP00-1", "100 000,00", "Альфа ООО")), db)

    second = _file(tmp_path, "вторая.csv", _row("IP00-1", "100 000,00", "Альфа ООО"),
                   _row("IP00-2", "50 000,00", "Бета ООО"))
    report = service.analyze_import(second, today=TODAY, db_path=db)

    assert report.unplanned == 1 and report.new == 1
    new = next(row for row in report.details if row.kind == "new")
    assert new.unplanned and "не было запланировано" in new.fields_text

    service.apply_import(report, today=TODAY, db_path=db, link=False)
    rows = _by_doc(db)
    assert rows["IP00-2"].unplanned is True
    assert rows["IP00-1"].unplanned is False


def test_заявка_на_место_плана_запланированной_считается(tmp_path, db):
    _import(_file(tmp_path, "первая.csv", _row("IP00-1", "100 000,00", "Альфа ООО")), db)
    store.save_payment(Payment(amount=50_000.0, recipient="Бета ООО",
                               pay_date=date(2026, 10, 20),
                               origin=PaymentOrigin.PLAN, origin_ref="план"), db)

    report = service.analyze_import(
        _file(tmp_path, "вторая.csv", _row("IP00-1", "100 000,00", "Альфа ООО"),
              _row("IP00-2", "55 000,00", "Бета ООО")), today=TODAY, db_path=db)

    assert report.adopted == 1 and report.unplanned == 0
    service.apply_import(report, today=TODAY, db_path=db, link=False)
    assert _by_doc(db)["IP00-2"].unplanned is False


def test_ручная_запись_тоже_план(tmp_path, db):
    _import(_file(tmp_path, "первая.csv", _row("IP00-1", "100 000,00", "Альфа ООО")), db)
    store.save_payment(Payment(amount=50_000.0, recipient="Бета ООО",
                               pay_date=date(2026, 10, 20)), db)

    report = service.analyze_import(
        _file(tmp_path, "вторая.csv", _row("IP00-1", "100 000,00", "Альфа ООО"),
              _row("IP00-2", "50 000,00", "Бета ООО")), today=TODAY, db_path=db)

    assert report.unplanned == 0


def test_план_на_другую_дату_не_спасает(tmp_path, db):
    """Заявка встала не туда, где её ждали, — запланированной по дате не была."""
    _import(_file(tmp_path, "первая.csv", _row("IP00-1", "100 000,00", "Альфа ООО")), db)
    store.save_payment(Payment(amount=50_000.0, recipient="Бета ООО",
                               pay_date=date(2026, 10, 22),
                               origin=PaymentOrigin.PLAN, origin_ref="план"), db)

    report = service.analyze_import(
        _file(tmp_path, "вторая.csv", _row("IP00-1", "100 000,00", "Альфа ООО"),
              _row("IP00-2", "55 000,00", "Бета ООО", pay="20.10.2026")),
        today=TODAY, db_path=db)

    assert report.unplanned == 1


def test_налоги_и_аренда_планом_не_бывают(tmp_path, db):
    _import(_file(tmp_path, "первая.csv", _row("IP00-1", "100 000,00", "Альфа ООО")), db)

    report = service.analyze_import(
        _file(tmp_path, "вторая.csv", _row("IP00-1", "100 000,00", "Альфа ООО"),
              _row("IP00-3", "70 000,00", "ИФНС", operation="Перечисление налогов и взносов")),
        today=TODAY, db_path=db)

    assert report.new == 1 and report.unplanned == 0


def test_повторная_выгрузка_пометку_не_снимает_и_не_ставит(tmp_path, db):
    _import(_file(tmp_path, "первая.csv", _row("IP00-1", "100 000,00", "Альфа ООО")), db)
    second = _file(tmp_path, "вторая.csv", _row("IP00-1", "100 000,00", "Альфа ООО"),
                   _row("IP00-2", "50 000,00", "Бета ООО"))
    _import(second, db)

    # Заявка IP00-2 изменилась в 1С: меняются данные, а пометка остаётся.
    third = _file(tmp_path, "третья.csv", _row("IP00-1", "100 000,00", "Альфа ООО"),
                  _row("IP00-2", "60 000,00", "Бета ООО"))
    report = service.analyze_import(third, today=TODAY, db_path=db)
    assert report.unplanned == 0
    service.apply_import(report, today=TODAY, db_path=db, link=False)

    rows = _by_doc(db)
    assert rows["IP00-2"].amount == 60_000.0
    assert rows["IP00-2"].unplanned is True
    assert rows["IP00-1"].unplanned is False


def test_правка_карточки_пометку_не_теряет(tmp_path, db):
    _import(_file(tmp_path, "первая.csv", _row("IP00-1", "100 000,00", "Альфа ООО")), db)
    _import(_file(tmp_path, "вторая.csv", _row("IP00-1", "100 000,00", "Альфа ООО"),
                  _row("IP00-2", "50 000,00", "Бета ООО")), db)
    payment = _by_doc(db)["IP00-2"]
    payment.comment = "согласовано"

    store.save_payment(payment, db)

    assert _by_doc(db)["IP00-2"].unplanned is True


def test_локальная_база_старой_версии_получает_колонку(db):
    import sqlite3

    from app.core.payments import schema

    connection = sqlite3.connect(db)
    for step in (schema._step_1, schema._step_2, schema._step_3):
        step(connection)
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    connection.close()

    saved = store.save_payment(Payment(amount=1.0, recipient="А", unplanned=True), db)

    assert store.get_payment(saved.id, db).unplanned is True


def test_миграция_сервера_добавляет_колонку():
    sql = (Path(__file__).resolve().parents[1]
           / "server/db/migrations/011_unplanned.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS unplanned BOOLEAN NOT NULL DEFAULT FALSE" in sql


def test_сервер_получает_пометку_новой_заявки():
    from app.core.payments import remote

    payload = remote._for_server(Payment(amount=1.0, recipient="А", unplanned=True,
                                         doc_number="IP00-9"))

    assert payload["unplanned"] is True


def test_ответ_сервера_без_пометки_читается():
    from app.core.payments import remote

    row = {"id": 1, "doc_number": "", "request_date": None, "pay_date": None,
           "amount": 1, "vat": 0, "currency": "руб.", "supplier_id": 0,
           "recipient": "А", "status": "planned", "source_status": "",
           "paid_flag": False, "operation": "", "over_limit": False, "priority": "",
           "edo_state": "", "responsible": "", "author": "", "comment": "",
           "had_files": False, "origin": "manual", "origin_ref": "",
           "created_at": "2026-10-05T10:00:00", "updated_at": "2026-10-05T10:00:00"}

    assert remote._payment(row).unplanned is False
    assert remote._payment({**row, "unplanned": True}).unplanned is True


def test_выгрузка_локальной_базы_несёт_пометку():
    from app.core.payments import sync

    assert sync.pack(Payment(amount=1.0, recipient="А", unplanned=True))["unplanned"] is True


# --- отображение ---------------------------------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def application():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_день_считает_незапланированные():
    day = date(2026, 10, 20)
    data = Day(day=day, payments=[Payment(amount=1.0, recipient="А", unplanned=True),
                                  Payment(amount=1.0, recipient="Б")])

    assert data.unplanned == 1


def test_на_клетке_дня_точка_незапланированной(application):
    from app.ui.widgets.calendar_grid import DayCell

    day = date(2026, 10, 20)
    cell = DayCell()
    cell.show_day(day, Day(day=day, payments=[Payment(amount=1.0, recipient="А", unplanned=True)]))

    assert not cell.unplanned.isHidden()
    assert "не было запланировано: 1" in cell.toolTip()
    assert cell.unplanned.size() == cell.alert.size()

    cell.show_day(day, Day(day=day, payments=[Payment(amount=1.0, recipient="А")]))
    assert cell.unplanned.isHidden()


def test_таблица_подписывает_незапланированное(application):
    from PySide6.QtCore import Qt

    from app.ui import payments_page as module
    from app.ui.widgets.table import ObjectTableModel

    columns = module.PaymentsPage._columns(None)
    model = ObjectTableModel(columns)
    model.set_items([Payment(amount=1.0, recipient="А", unplanned=True),
                     Payment(amount=1.0, recipient="Б")])
    index = next(i for i, c in enumerate(columns) if c.title == "План")

    assert model.data(model.index(0, index), Qt.ItemDataRole.DisplayRole) == "Не было запланировано"
    assert model.data(model.index(1, index), Qt.ItemDataRole.DisplayRole) == ""


def test_окно_импорта_показывает_и_отбирает_незапланированные(application, tmp_path, db):
    from app.ui.widgets.payment_dialogs import ImportDialog

    _import(_file(tmp_path, "первая.csv", _row("IP00-1", "100 000,00", "Альфа ООО")), db)
    report = service.analyze_import(
        _file(tmp_path, "вторая.csv", _row("IP00-1", "100 000,00", "Альфа ООО"),
              _row("IP00-2", "50 000,00", "Бета ООО")), today=TODAY, db_path=db)

    dialog = ImportDialog(db_path=db)
    dialog._ready(report)
    dialog.kind_filter.setCurrentIndex(dialog.kind_filter.findData("unplanned"))

    shown = dialog._shown_details()
    assert [row.doc_number for row in shown] == ["IP00-2"]
