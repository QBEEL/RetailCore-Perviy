"""Выгрузка локальной базы в общую: опознание записей, отчёт, отправка.

Сервер не поднимается — вместо него подставляется `remote`. Проверяется то, что
решает приложение: какая запись какой соответствует, что считается
расхождением и что уходит на сервер, а что не уходит никогда.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.payments import (
    Budget,
    Payment,
    PaymentOrigin,
    PaymentStatus,
    plan,
    remote,
    service,
    store,
    sync,
    transport,
)

DAY = date(2026, 10, 5)


def payment(**fields) -> Payment:
    base = dict(amount=100000.0, pay_date=DAY, recipient="Суперкосметикс ООО",
                responsible="Иванов Евгений", operation="Оплата поставщику")
    base.update(fields)
    return Payment(**base)


def from_1c(number: str, **fields) -> Payment:
    return payment(doc_number=number, request_date=date(2026, 9, 20),
                   origin=PaymentOrigin.IMPORT, **fields)


def from_plan(**fields) -> Payment:
    return payment(origin=PaymentOrigin.PLAN,
                   origin_ref=plan.plan_ref("Иванов Евгений", 2026, 10), **fields)


# --- опознание записей --------------------------------------------------------

def test_строка_из_1с_узнаётся_по_номеру_и_дате_заявки():
    """Тот же ключ, что у импорта: две базы не должны расходиться в правилах."""
    mine = from_1c("IP00-000001", amount=100000.0)
    theirs = from_1c("IP00-000001", amount=250000.0, recipient="ООО Суперкосметикс")

    assert sync.key_of(mine) == sync.key_of(theirs)


def test_план_узнаётся_по_метке_поставщику_и_дате():
    assert sync.key_of(from_plan()) == sync.key_of(from_plan(amount=999.0))
    # Другой день — другая строка плана.
    assert sync.key_of(from_plan()) != sync.key_of(from_plan(pay_date=date(2026, 10, 6)))


def test_ручная_оплата_узнаётся_по_получателю_дате_и_ответственному():
    mine = payment(comment="счёт 114")
    theirs = payment(comment="другой комментарий")

    assert sync.key_of(mine) == sync.key_of(theirs)
    assert sync.key_of(mine) != sync.key_of(payment(pay_date=date(2026, 10, 6)))
    assert sync.key_of(mine) != sync.key_of(payment(responsible="Валева Карина"))


def test_исправленная_сумма_это_правка_а_не_вторая_оплата():
    """С суммой в ключе выгрузка удвоила бы день в бюджете отдела."""
    assert sync.key_of(payment(amount=100000.0)) == sync.key_of(payment(amount=250000.0))


def test_написание_имени_получателя_не_разводит_записи():
    mine = payment(recipient='ООО "Суперкосметикс"')
    theirs = payment(recipient="Суперкосметикс ООО")

    assert sync.key_of(mine) == sync.key_of(theirs)


# --- сравнение ----------------------------------------------------------------

def test_отчёт_делит_записи_на_новые_совпавшие_и_расхождения():
    mine = [from_1c("IP00-000001"), from_1c("IP00-000002", amount=500000.0), from_plan()]
    theirs = [from_1c("IP00-000001", id=11),
              from_1c("IP00-000002", id=12, amount=480000.0)]

    report = sync.compare(mine, theirs)

    assert len(report.same) == 1
    assert len(report.created) == 1 and report.created[0].origin is PaymentOrigin.PLAN
    assert len(report.updated) == 1
    assert report.updated[0].fields == ("amount",)
    assert report.updated[0].remote.id == 12


def test_серверные_записи_которых_нет_у_меня_не_трогаются():
    """Пока администратор работал у себя, отдел работал в общей базе."""
    mine = [from_1c("IP00-000001")]
    theirs = [from_1c("IP00-000001", id=11),
              payment(id=12, recipient="Чужая оплата коллеги")]

    report = sync.compare(mine, theirs)

    assert report.only_remote == 1
    assert report.changes == 0


def test_одинаковые_строки_разбираются_по_порядку():
    """Три моих против одной серверной — одно совпадение и две новых."""
    mine = [payment(comment="раз"), payment(comment="два"), payment(comment="три")]
    theirs = [payment(id=11)]

    report = sync.compare(mine, theirs)

    assert len(report.created) == 2
    assert len(report.updated) == 1


def test_расхождение_называет_поля():
    mine = [payment(amount=120000.0, status=PaymentStatus.PAID, comment="оплачено")]
    theirs = [payment(id=11, amount=100000.0, status=PaymentStatus.PLANNED)]

    report = sync.compare(mine, theirs)

    assert set(report.updated[0].fields) == {"amount", "status", "comment"}
    assert "сумма" in report.updated[0].what and "статус" in report.updated[0].what


def test_копейки_расхождением_не_считаются():
    mine = [payment(amount=100000.001)]
    theirs = [payment(id=11, amount=100000.0)]

    assert not sync.compare(mine, theirs).updated


def test_бюджеты_уходят_только_изменившиеся():
    local = [Budget(year=2026, month=10, amount=8400000.0),
             Budget(year=2026, month=11, amount=5000000.0),
             Budget(year=2026, month=12, amount=0.0)]
    server = [Budget(year=2026, month=10, amount=8400000.0),
              Budget(year=2026, month=11, amount=4000000.0)]

    report = sync.compare([], [], local_budgets=local, remote_budgets=server)

    # Октябрь совпал, декабрь пуст, ноябрь отличается — уходит он один.
    assert [(b.month, b.amount) for b in report.budgets] == [(11, 5000000.0)]


# --- сценарий выгрузки --------------------------------------------------------

@pytest.fixture
def base(tmp_path, monkeypatch):
    """Локальная база, вошедший администратор и подставной сервер."""
    path = str(tmp_path / "payments.db")
    monkeypatch.setattr(store, "database_path", lambda: path)
    monkeypatch.setattr(transport.session, "token", "x")
    monkeypatch.setattr(transport.session, "base_url", "https://example")
    monkeypatch.setattr(transport.session, "is_admin", True)

    server: dict = {"payments": [], "budgets": [], "sent": []}
    monkeypatch.setattr(remote, "list_payments", lambda *a, **k: list(server["payments"]))
    monkeypatch.setattr(remote, "budgets", lambda *a, **k: list(server["budgets"]))

    def upload(created, changed, budgets=()):
        server["sent"].append({"created": list(created), "changed": list(changed),
                               "budgets": list(budgets)})
        return {"new": len(created), "updated": len(changed), "budgets": len(budgets)}

    monkeypatch.setattr(remote, "upload", upload)
    return {"db": path, "server": server}


def test_выгружается_всё_чего_нет_на_сервере(base):
    store.save_payment(from_1c("IP00-000001"), base["db"])
    store.save_payment(from_plan(amount=480000.0, pay_date=date(2026, 10, 12)), base["db"])
    store.save_budget(Budget(year=2026, month=10, amount=8400000.0), base["db"])

    report = service.analyze_upload(db_path=base["db"])

    assert len(report.created) == 2
    assert len(report.budgets) == 1
    assert report.changes == 3

    service.apply_upload(report)
    sent = base["server"]["sent"]
    rows = [row for part in sent for row in part["created"]]
    assert {row["origin"] for row in rows} == {"import", "plan"}
    assert report.applied and report.written == 2


def test_расхождение_решается_в_пользу_локальной_записи(base):
    store.save_payment(from_1c("IP00-000001", amount=250000.0), base["db"])
    base["server"]["payments"] = [from_1c("IP00-000001", id=77, amount=100000.0)]

    report = service.analyze_upload(db_path=base["db"])
    service.apply_upload(report)

    changed = [row for part in base["server"]["sent"] for row in part["changed"]]
    assert len(changed) == 1
    assert changed[0]["id"] == 77
    assert changed[0]["payment"]["amount"] == 250000.0


def test_повторная_выгрузка_ничего_не_отправляет(base):
    store.save_payment(from_1c("IP00-000001"), base["db"])
    base["server"]["payments"] = [from_1c("IP00-000001", id=77)]

    report = service.analyze_upload(db_path=base["db"])

    assert report.changes == 0
    assert len(report.same) == 1


def test_не_администратору_выгрузка_недоступна(base, monkeypatch):
    monkeypatch.setattr(transport.session, "is_admin", False)

    with pytest.raises(service.UploadNotAllowed, match="администратору"):
        service.analyze_upload(db_path=base["db"])


def test_без_входа_выгружать_некуда(base, monkeypatch):
    monkeypatch.setattr(transport.session, "token", "")

    with pytest.raises(service.UploadNotAllowed, match="вход"):
        service.analyze_upload(db_path=base["db"])


def test_большая_выгрузка_уходит_частями(base, monkeypatch):
    monkeypatch.setattr(service, "BATCH", 2)
    for number in range(5):
        store.save_payment(from_1c(f"IP00-00000{number}"), base["db"])

    report = service.analyze_upload(db_path=base["db"])
    service.apply_upload(report)

    parts = [part for part in base["server"]["sent"] if part["created"]]
    assert [len(part["created"]) for part in parts] == [2, 2, 1]


# --- переключение базы --------------------------------------------------------

def test_локальный_режим_переключает_источник_данных(monkeypatch):
    from app.core.payments import data

    monkeypatch.setattr(transport.session, "token", "x")
    monkeypatch.setattr(transport.session, "base_url", "https://example")
    try:
        data.set_local_only(False)
        assert data.online() and data.backend() is remote

        data.set_local_only(True)
        # Вход при этом сохраняется: поставщики и личный кабинет остаются.
        assert not data.online() and data.backend() is store
        assert transport.session.active
    finally:
        data.set_local_only(False)
