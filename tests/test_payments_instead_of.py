"""Оплата «вместо другого поставщика»: хранение, обмен с сервером и карточка.

Сервер не поднимается: вместо него подставляется `transport`. Проверяется то,
что решает приложение, — что записать, что отправить и чего не затирать.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.core.payments import (
    Payment,
    PaymentOrigin,
    PaymentStatus,
    remote,
    replacement,
    schema,
    store,
    sync,
    transport,
)


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def database(monkeypatch, tmp_path):
    path = str(tmp_path / "payments.db")
    monkeypatch.setattr(store, "database_path", lambda: path)
    return path


@pytest.fixture
def server(monkeypatch):
    """Вошедший пользователь и перехват запросов к серверу."""
    log: list[tuple[str, str, object]] = []
    answers: dict[str, object] = {}

    def reply(method, path, body=None):
        log.append((method, path, body))
        return answers.get(path) or {}

    monkeypatch.setattr(transport, "post", lambda path, body=None, params=None: reply("POST", path, body))
    monkeypatch.setattr(transport, "patch", lambda path, body=None: reply("PATCH", path, body))
    monkeypatch.setattr(transport.session, "base_url", "https://retail.test")
    monkeypatch.setattr(transport.session, "token", "тестовый")
    return log, answers


def _server_row(**fields) -> dict:
    row = {"id": 7, "doc_number": "", "request_date": None, "pay_date": "2026-10-05",
           "amount": 100, "vat": 0, "currency": "руб.", "supplier_id": 0,
           "recipient": "Альфа", "status": "planned", "source_status": "",
           "paid_flag": False, "operation": "", "over_limit": False, "priority": "",
           "edo_state": "", "responsible": "", "author": "", "comment": "",
           "had_files": False, "origin": "manual", "origin_ref": "",
           "created_at": "2026-10-05T10:00:00", "updated_at": "2026-10-05T10:00:00"}
    row.update(fields)
    return row


# --- локальная база ------------------------------------------------------------

def test_локальная_база_хранит_кого_заменили(database):
    saved = store.save_payment(Payment(amount=100.0, recipient="Альфа",
                                       pay_date=date(2026, 10, 5), instead_of="Бета"))

    assert store.get_payment(saved.id).instead_of == "Бета"


def test_отметку_можно_снять(database):
    saved = store.save_payment(Payment(amount=100.0, recipient="Альфа", instead_of="Бета"))
    saved.instead_of = ""
    store.save_payment(saved)

    assert store.get_payment(saved.id).instead_of == ""


def test_локальная_база_старой_версии_получает_колонку(database):
    connection = sqlite3.connect(database)
    for step in (schema._step_1, schema._step_2, schema._step_3, schema._step_4):
        step(connection)
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()

    # Любое открытие базы доводит схему до актуальной.
    saved = store.save_payment(Payment(amount=1.0, recipient="Альфа", instead_of="Бета"))

    assert store.get_payment(saved.id).instead_of == "Бета"


def test_повторный_импорт_не_стирает_отметку(database):
    """Выгрузка 1С поля не содержит — проставленное человеком остаётся."""
    old = Payment(amount=100.0, recipient="Альфа", doc_number="IP00-1",
                  request_date=date(2026, 9, 1), origin=PaymentOrigin.IMPORT,
                  instead_of="Бета")
    saved = store.save_payment(old)

    fresh = Payment(amount=150.0, recipient="Альфа", doc_number="IP00-1",
                    request_date=date(2026, 9, 1), origin=PaymentOrigin.IMPORT)
    store.apply_import([], [(saved.id, fresh)])

    row = store.get_payment(saved.id)
    assert row.amount == 150.0
    assert row.instead_of == "Бета"


def test_поиск_находит_оплату_по_заменённому_поставщику(database):
    store.save_payment(Payment(amount=1.0, recipient="Альфа", instead_of="Невалайн"))
    store.save_payment(Payment(amount=2.0, recipient="Гамма"))

    found = store.list_payments(store.Filter(text="Невалайн"))

    assert [p.recipient for p in found] == ["Альфа"]


# --- сервер --------------------------------------------------------------------

def test_сервер_получает_отметку_при_создании_и_правке(server):
    log, answers = server
    row = _server_row(instead_of="Бета")
    answers["/api/payments"] = row
    answers["/api/payments/7"] = row

    created = remote.save_payment(Payment(amount=100.0, recipient="Альфа",
                                          pay_date=date(2026, 10, 5), instead_of="Бета"))
    remote.save_payment(created)

    assert log[0][2]["instead_of"] == "Бета"
    assert log[1][2]["instead_of"] == "Бета"
    assert created.instead_of == "Бета"


def test_ответ_старого_сервера_без_отметки_читается():
    assert remote._payment(_server_row()).instead_of == ""


# --- выгрузка локальной базы ---------------------------------------------------

def test_выгрузка_передаёт_отметку():
    assert sync.pack(Payment(amount=1.0, recipient="А", instead_of="Б"))["instead_of"] == "Б"


def test_пустая_отметка_у_меня_не_расхождение():
    mine = Payment(amount=1.0, recipient="А")
    theirs = Payment(amount=1.0, recipient="А", instead_of="Б")

    assert "instead_of" not in sync.differences(mine, theirs)


def test_моя_отметка_против_серверной_расхождение():
    mine = Payment(amount=1.0, recipient="А", instead_of="В")
    theirs = Payment(amount=1.0, recipient="А", instead_of="Б")

    assert "instead_of" in sync.differences(mine, theirs)


# --- карточка оплаты -----------------------------------------------------------

@pytest.fixture
def dialog_for(application, database, monkeypatch):
    from app.core.payments import data
    from app.ui.widgets import payment_dialogs
    from app.ui.widgets.payment_dialogs import PaymentDialog

    monkeypatch.setattr(data, "online", lambda: False)
    # История по поставщику грузится в потоке и читает базу уже после теста.
    monkeypatch.setattr(payment_dialogs, "run_task", lambda *args, **kwargs: None)

    def make(payment: Payment):
        return PaymentDialog(payment, recipients=["Альфа", "Бета"], responsible=[],
                             operations=[], dds_items=["Аренда"])
    return make


def _ready(**fields) -> Payment:
    return Payment(amount=10.0, recipient="Альфа", pay_date=date(2026, 10, 5), **fields)


def test_у_обычной_оплаты_поле_пустое(dialog_for):
    dialog = dialog_for(_ready())

    assert dialog.instead_of.currentText() == ""
    assert dialog.result_payment().instead_of == ""


def test_открытая_оплата_показывает_кого_заменили(dialog_for):
    assert dialog_for(_ready(id=5, instead_of="Бета")).instead_of.currentText() == "Бета"


def _beta(amount: float = 500_000.0, **fields) -> Payment:
    return Payment(id=41, amount=amount, vat=amount / 6, recipient="Бета",
                   pay_date=date(2026, 10, 7), **fields)


def test_выбранный_поставщик_попадает_в_оплату(dialog_for):
    dialog = dialog_for(_ready())
    dialog.instead_of.setCurrentText("Бета")
    dialog._instead_changed()
    dialog._show_targets(("Бета", [_beta()]))

    dialog._accept()

    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.result_payment().instead_of == "Бета"


def test_пробелы_вокруг_названия_обрезаются(dialog_for):
    dialog = dialog_for(_ready())
    dialog.instead_of.setCurrentText("  Бета  ")

    assert dialog.result_payment().instead_of == "Бета"


def test_вместо_самого_себя_не_сохраняется(dialog_for):
    dialog = dialog_for(_ready())
    dialog.instead_of.setCurrentText("Альфа")

    dialog._accept()

    assert dialog.result() != dialog.DialogCode.Accepted
    assert "самого себя" in dialog.error.text()


# --- что делать с заменяемой оплатой -------------------------------------------

def _choose(dialog, *options: Payment, name: str = "Бета") -> None:
    dialog.instead_of.setCurrentText(name)
    dialog._instead_changed()
    dialog._show_targets((name, list(options)))


def test_пока_оплаты_заменяемого_не_загружены_сохранить_нельзя(dialog_for):
    dialog = dialog_for(_ready())
    dialog.instead_of.setCurrentText("Бета")
    dialog._instead_changed()

    dialog._accept()

    assert dialog.result() != dialog.DialogCode.Accepted
    assert "Загружаются" in dialog.error.text()


def test_у_заменяемого_нет_неоплаченных_оплат(dialog_for):
    dialog = dialog_for(_ready())
    _choose(dialog)

    dialog._accept()

    assert dialog.result() != dialog.DialogCode.Accepted
    assert "нет неоплаченных" in dialog.error.text()


def test_сумма_больше_заменяемой_не_даёт_заменить(dialog_for):
    dialog = dialog_for(_ready())
    dialog.amount.setValue(600_000.0)
    _choose(dialog, _beta(500_000.0))

    dialog._accept()

    assert dialog.result() != dialog.DialogCode.Accepted
    assert "заменить её нельзя" in dialog.error.text()


def test_меньшая_сумма_по_умолчанию_сокращает(dialog_for):
    dialog = dialog_for(_ready())
    dialog.amount.setValue(120_000.0)
    _choose(dialog, _beta(500_000.0))

    target, mode = dialog.replacement_choice()

    assert target.id == 41
    assert mode is replacement.Mode.REDUCE
    assert "380 000" in dialog.replace_result.text()


def test_равная_сумма_только_отменяет(dialog_for):
    dialog = dialog_for(_ready())
    dialog.amount.setValue(500_000.0)
    _choose(dialog, _beta(500_000.0))

    assert dialog.replace_reduce.isEnabled() is False
    assert dialog.replacement_choice()[1] is replacement.Mode.CANCEL

    dialog._accept()
    assert dialog.result() == dialog.DialogCode.Accepted


def test_можно_выбрать_отмену_вместо_сокращения(dialog_for):
    dialog = dialog_for(_ready())
    dialog.amount.setValue(120_000.0)
    _choose(dialog, _beta(500_000.0))

    dialog.replace_cancel.setChecked(True)

    assert dialog.replacement_choice()[1] is replacement.Mode.CANCEL
    assert "отменена" in dialog.replace_result.text()


def test_по_умолчанию_выбрана_ближайшая_по_дате_оплата(dialog_for):
    dialog = dialog_for(_ready())
    dialog.pay_date.set_value(date(2026, 11, 20))
    far = _beta(300_000.0)
    near = Payment(id=42, amount=400_000.0, recipient="Бета", pay_date=date(2026, 11, 18))
    _choose(dialog, far, near)

    assert dialog.replacement_choice()[0].id == 42


def test_заменяемая_у_открытой_оплаты_не_спрашивается_повторно(dialog_for):
    """Поле не менялось — оплата уже заменила другую раньше."""
    dialog = dialog_for(_ready(id=5, instead_of="Бета"))

    dialog._instead_changed()

    assert dialog.replacement_choice() is None
    dialog._accept()
    assert dialog.result() == dialog.DialogCode.Accepted


# --- расчёт замены -------------------------------------------------------------

def test_кандидаты_только_ждущие_оплаты_этого_поставщика():
    rows = [
        Payment(id=1, amount=1.0, recipient="ООО Бета", status=PaymentStatus.PLANNED,
                pay_date=date(2026, 10, 20)),
        Payment(id=2, amount=1.0, recipient="Бета ООО", status=PaymentStatus.PLANNED,
                pay_date=date(2026, 10, 10)),
        Payment(id=3, amount=1.0, recipient="Бета", status=PaymentStatus.PAID),
        Payment(id=4, amount=1.0, recipient="Бета", status=PaymentStatus.CANCELLED),
        Payment(id=5, amount=1.0, recipient="Альфа", status=PaymentStatus.PLANNED),
        Payment(id=6, amount=1.0, recipient="Бета", status=PaymentStatus.PLANNED,
                operation="Аренда"),
    ]

    assert [p.id for p in replacement.candidates(rows, "Бета")] == [2, 1]
    assert [p.id for p in replacement.candidates(rows, "Бета", own_id=2)] == [1]


def test_сокращение_уменьшает_сумму_и_ндс_и_пишет_причину():
    target = Payment(id=1, amount=500_000.0, vat=50_000.0, recipient="Бета", comment="счёт 12")
    new = Payment(amount=125_000.0, recipient="Альфа", pay_date=date(2026, 10, 9))

    changed = replacement.apply(target, new, replacement.Mode.REDUCE)

    assert changed.amount == 375_000.0
    assert changed.vat == 37_500.0
    assert changed.status is target.status
    assert changed.comment.startswith("счёт 12\n")
    assert "Альфа" in changed.comment and "09.10.2026" in changed.comment
    assert target.amount == 500_000.0  # исходная запись не тронута


def test_отмена_меняет_статус():
    target = Payment(id=1, amount=500_000.0, recipient="Бета")
    new = Payment(amount=1_000.0, recipient="Альфа")

    changed = replacement.apply(target, new, replacement.Mode.CANCEL)

    assert changed.status is PaymentStatus.CANCELLED
    assert changed.amount == 500_000.0


def test_замена_больше_оплаты_отклоняется():
    target = Payment(id=1, amount=100.0, recipient="Бета")

    with pytest.raises(ValueError):
        replacement.apply(target, Payment(amount=100.01, recipient="Альфа"),
                          replacement.Mode.CANCEL)


def test_замена_в_локальной_базе_доходит_до_конца(database):
    beta = store.save_payment(Payment(amount=500_000.0, vat=50_000.0, recipient="Бета",
                                      pay_date=date(2026, 10, 7)))
    new = store.save_payment(Payment(amount=200_000.0, recipient="Альфа",
                                     pay_date=date(2026, 10, 8), instead_of="Бета"))

    store.save_payment(replacement.apply(beta, new, replacement.Mode.REDUCE))

    saved = store.get_payment(beta.id)
    assert saved.amount == 300_000.0
    assert saved.amount_before == 500_000.0  # правка помечена, как любая ручная
