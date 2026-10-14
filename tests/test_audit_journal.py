"""Журнал изменений: по записи видно, что это за оплата и что в ней поменялось."""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.payments import admin

# Серверный модуль не зависит от базы, пока не вызвана `record`, — поэтому его
# можно проверять здесь, не поднимая сервер.
_SPEC = importlib.util.spec_from_file_location(
    "auditlog", Path(__file__).resolve().parents[1] / "server/api/app/auditlog.py")
auditlog = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(auditlog)


# --- сервер: что попадает в журнал ----------------------------------------------

def test_в_журнал_идёт_только_изменённое_и_прежнее_значение():
    old = {"amount": Decimal("100000.00"), "status": "planned", "comment": "",
           "pay_date": date(2026, 10, 14), "priority": ""}
    sent = {"amount": 90000.0, "status": "planned", "comment": None,
            "pay_date": date(2026, 10, 14), "priority": ""}

    changes = auditlog.diff(old, sent)

    assert changes == {"amount": 90000.0, "was": {"amount": 100000.0}}


def test_повторная_отправка_тех_же_значений_не_создаёт_записи():
    old = {"amount": Decimal("100.00"), "status": "paid", "vat": Decimal("0")}

    assert auditlog.diff(old, {"amount": 100.004, "status": "paid", "vat": 0.0}) == {}


def test_снятая_дата_видна_как_изменение():
    changes = auditlog.diff({"pay_date": date(2026, 10, 14)}, {"pay_date": None})

    assert changes == {"pay_date": None, "was": {"pay_date": "2026-10-14"}}


def test_оплата_описывается_словами():
    about = auditlog.snapshot({
        "recipient": "Шкурная Юлия Александровна ИП", "amount": Decimal("100000.00"),
        "pay_date": date(2026, 10, 14), "doc_number": "", "comment": "лишнее"})

    assert auditlog.describe(about) == \
        "Шкурная Юлия Александровна ИП · 100 000 ₽ · 14.10.2026"


def test_сумма_с_копейками_и_номер_документа():
    text = auditlog.describe({"recipient": "ООО Седлко", "amount": 1234.5,
                              "doc_number": "А-17"})

    assert text == "ООО Седлко · 1 234,5 ₽ · док. А-17"


# --- клиент: как это видно в таблице --------------------------------------------

def _entry(**fields) -> admin.Entry:
    return admin.Entry(id=1, user="Иванов", entity="оплата", **fields)


def test_запись_показывает_номер_и_что_за_оплата():
    entry = _entry(entity_id=22122, action="создание",
                   subject="Шкурная Юлия Александровна ИП · 100 000 ₽")

    assert entry.record == "№22122 · Шкурная Юлия Александровна ИП · 100 000 ₽"


def test_сервер_прежней_версии_даёт_просто_номер():
    assert _entry(entity_id=22122).record == "№22122"
    assert _entry().record == ""


def test_изменение_показывается_как_было_стало():
    entry = _entry(changes={
        "amount": 90000.0, "status": "moved", "pay_date": "2026-10-15",
        "was": {"amount": 100000.0, "status": "planned", "pay_date": "2026-10-14"}})

    assert entry.summary == (
        "сумма: 100 000 → 90 000 · статус: Запланировано → Перенесено"
        " · дата оплаты: 14.10.2026 → 15.10.2026")


def test_прежняя_сумма_из_старых_записей_тоже_читается():
    entry = _entry(changes={"amount": 90000.0, "amount_was": 100000.0})

    assert entry.summary == "сумма: 100 000 → 90 000"


def test_получатель_не_повторяется_если_он_уже_в_записи():
    entry = _entry(subject="Седлко ООО · 5 000 ₽",
                   changes={"recipient": "Седлко ООО", "responsible": "Валева Карина"})

    assert entry.summary == "ответственный: Валева Карина"


def test_удалённая_оплата_оставляет_след_в_изменениях():
    entry = _entry(entity_id=22121, action="удаление", subject="Шкурная ИП · 100 000 ₽",
                   changes={"status": "planned", "comment": "", "responsible": "Иванов"})

    assert entry.summary == "статус: Запланировано · комментарий: — · ответственный: Иванов"


# --- пометки «стоит проверить» -----------------------------------------------------

from datetime import datetime

import pytest
from PySide6.QtWidgets import QApplication

from app.core.payments import transport


def _payment(**fields) -> admin.Entry:
    fields.setdefault("entity_id", 22122)
    fields.setdefault("at", datetime(2026, 10, 14, 11, 30))  # среда
    return admin.Entry(id=1, user="Иванов", entity="оплата", kind="payment", **fields)


def test_удаление_оплаты_помечается():
    assert _payment(action="удаление").flags == ["оплата удалена"]


def test_правка_суммы_помечается_с_порога_двадцати_процентов():
    big = _payment(action="изменение",
                   changes={"amount": 80000.0, "was": {"amount": 100000.0}})
    small = _payment(action="изменение",
                     changes={"amount": 90000.0, "was": {"amount": 100000.0}})

    assert big.flags == ["сумма изменена на 20 %"]
    assert small.flags == []


def test_прежняя_сумма_из_старой_записи_тоже_считается():
    entry = _payment(action="изменение", changes={"amount": 150.0, "amount_was": 100.0})

    assert entry.flags == ["сумма изменена на 50 %"]


def test_оплата_без_карточки_поставщика_и_в_выходной_помечается():
    saturday = datetime(2026, 10, 17, 10, 0)

    entry = _payment(action="создание", no_supplier=True, at=saturday)

    assert entry.flags == ["нет карточки поставщика", "заведена в выходной"]


def test_обычное_создание_в_будни_не_помечается():
    assert _payment(action="создание").flags == []


def test_не_оплаты_не_помечаются():
    entry = admin.Entry(id=1, entity="учётная запись", kind="app_user", action="удаление")

    assert entry.flags == []


def test_сервер_прежней_версии_без_кода_сущности_всё_равно_понят():
    entry = admin.Entry(id=1, entity="оплата", action="удаление")

    assert entry.is_payment and entry.flags == ["оплата удалена"]


# --- окно записи --------------------------------------------------------------------

def test_строки_окна_записи_полные_и_списки_разворачиваются():
    entry = _payment(action="изменение", changes={
        "amount": 90000.0, "comment": "перенос по просьбе поставщика",
        "was": {"amount": 100000.0, "comment": ""}})
    bulk = admin.Entry(id=2, entity="оплата", kind="payment", changes={
        "ids": 2, "touched": ["№1 А · 5 ₽", "№2 Б · 7 ₽"]})

    assert entry.rows() == [
        ("сумма", "100 000", "90 000"),
        ("комментарий", "—", "перенос по просьбе поставщика")]
    assert bulk.rows() == [("записей", "", "2"), ("оплаты", "", "№1 А · 5 ₽"),
                           ("", "", "№2 Б · 7 ₽")]


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def test_окно_записи_показывает_изменения_и_только_историю_своей_оплаты(
        application, monkeypatch):
    from app.ui.widgets import journal_dialog

    mine = [_payment(action="создание"), _payment(action="удаление")]
    mine[1].id = 2
    foreign = _payment(action="создание", entity_id=999)
    seen = {}

    def fake_journal(**query):
        seen.update(query)
        return [*mine, foreign]

    monkeypatch.setattr(admin, "journal", fake_journal)
    monkeypatch.setattr(
        journal_dialog, "run_task",
        lambda function, *args, on_result=None, on_error=None, **kw: on_result(
            function(*args)))

    dialog = journal_dialog.JournalEntryDialog(mine[1])

    # Условие по номеру уходит серверу; чужая оплата из ответа отброшена — на
    # случай сервера прежней версии, который отдал бы весь журнал.
    assert seen["record_id"] == 22122 and seen["entity"] == "payment"
    assert dialog.history_table.model_.rowCount() == 2
    assert dialog.changes_table.model_.rowCount() == len(mine[1].rows())


def test_окно_не_оплаты_не_грузит_историю(application, monkeypatch):
    from app.ui.widgets import journal_dialog

    called = []
    monkeypatch.setattr(journal_dialog, "run_task", lambda *a, **k: called.append(1))
    entry = admin.Entry(id=1, entity="бюджет", kind="budget", entity_id=202610,
                        action="сохранение", changes={"amount": 5})

    journal_dialog.JournalEntryDialog(entry)

    assert called == []


# --- отбор и выгрузка -----------------------------------------------------------------

def test_отбор_уходит_на_сервер_а_пустое_условие_не_отправляется(monkeypatch):
    sent = {}
    monkeypatch.setattr(transport, "get", lambda path, params=None: sent.update(
        path=path, params=params) or [])

    admin.journal(500, "payment", "valeva", record_id=22122, action="delete",
                  start=date(2026, 10, 1), end=date(2026, 10, 14), text=" Седлко ")

    assert sent["path"] == "/api/audit"
    assert sent["params"] == {
        "limit": 500, "entity": "payment", "login": "valeva", "record_id": 22122,
        "action": "delete", "text": "Седлко", "start": "2026-10-01",
        "end": "2026-10-14"}


def test_страница_журнала_отбирает_только_требующие_проверки(application):
    from app.core.settings import AppSettings
    from app.ui.admin_page import AdminPage

    page = AdminPage(AppSettings(), lambda *_: None)
    page.entries = [_payment(action="создание"), _payment(action="удаление")]

    page._render_journal()
    assert page.journal_table.model_.rowCount() == 2
    assert "требуют проверки: 1" in page.journal_count.text()

    page.journal_flagged.setChecked(True)
    assert page.journal_table.model_.rowCount() == 1


def test_запрос_журнала_по_умолчанию_без_ограничений_кроме_числа(application):
    from app.core.settings import AppSettings
    from app.ui.admin_page import AdminPage

    query = AdminPage(AppSettings(), lambda *_: None)._journal_query()

    assert query == {"limit": 200, "entity": "", "login": "", "action": "",
                     "text": "", "start": None}


def test_выгрузка_в_excel_содержит_пометки(tmp_path):
    import openpyxl

    from app.ui.admin_page import write_journal

    path = tmp_path / "journal.xlsx"
    write_journal([_payment(action="удаление", subject="Седлко ООО · 5 000 ₽")], str(path))

    sheet = openpyxl.load_workbook(path).active
    assert [c.value for c in sheet[1]][:2] == ["Когда", "Кто"]
    row = [c.value for c in sheet[2]]
    assert row[4] == "№22122 · Седлко ООО · 5 000 ₽" and row[6] == "оплата удалена"
