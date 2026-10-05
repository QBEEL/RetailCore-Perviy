"""Проверка дублей: ручная запись и заявка 1С на одни деньги."""
from __future__ import annotations

import sys
from datetime import date
from itertools import count
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.payments import Payment, PaymentOrigin, PaymentStatus, service, store
from app.core.payments.duplicates import DuplicateKind, find_duplicates
from tests.test_payments import TODAY, _csv, _row, db  # noqa: F401 — фикстура db

_ids = count(1)


def _manual(amount: float, pay: date, recipient: str = "НеваЛайн ООО", **fields) -> Payment:
    return Payment(id=next(_ids), amount=amount, pay_date=pay, recipient=recipient,
                   origin=PaymentOrigin.MANUAL, **fields)


def _request(amount: float, pay: date, number: str = "IP00-001625",
             recipient: str = "НеваЛайн ООО", **fields) -> Payment:
    return Payment(id=next(_ids), amount=amount, pay_date=pay, recipient=recipient,
                   doc_number=number, request_date=date(2026, 10, 20),
                   origin=PaymentOrigin.IMPORT, **fields)


def _kinds(found) -> list[DuplicateKind]:
    return [suspect.kind for suspect in found]


def test_сдвиг_даты_на_день_и_отклонение_суммы_находятся():
    manual = _manual(800_000, date(2026, 10, 24))
    request = _request(855_876, date(2026, 10, 25))
    (found,) = find_duplicates([manual, request])
    assert found.kind is DuplicateKind.DOUBLE
    assert found.manual == [manual] and found.requests == [request]
    assert found.shift_days == 1
    # Суммы разошлись больше чем на 5 %, а дата не совпала — это «проверьте».
    assert not found.confident and not found.recommended


def test_сдвиг_за_окно_или_сумма_за_допуском_не_дубль():
    manual = _manual(800_000, date(2026, 10, 24))
    assert not find_duplicates([manual, _request(800_000, date(2026, 10, 28))])
    assert not find_duplicates([manual, _request(1_200_000, date(2026, 10, 25))])


def test_та_же_дата_находится_при_любой_сумме_но_без_уверенности():
    manual = _manual(500_000, date(2026, 10, 24))
    (found,) = find_duplicates([manual, _request(900_000, date(2026, 10, 24))])
    assert found.kind is DuplicateKind.DOUBLE and not found.confident


def test_та_же_дата_и_близкая_сумма_отмечаются_по_умолчанию():
    manual = _manual(800_000, date(2026, 10, 24))
    (found,) = find_duplicates([manual, _request(855_876, date(2026, 10, 24))])
    assert found.confident and found.recommended


def test_две_ручные_части_и_одна_заявка():
    first = _manual(400_000, date(2026, 10, 24))
    second = _manual(455_000, date(2026, 10, 25))
    request = _request(855_000, date(2026, 10, 25))
    (found,) = find_duplicates([first, second, request])
    assert found.kind is DuplicateKind.SPLIT_LOCAL
    assert {p.id for p in found.manual} == {first.id, second.id}
    assert found.requests == [request]
    assert found.confident and found.delta == pytest.approx(0.0)


def test_одна_ручная_запись_и_две_заявки():
    manual = _manual(855_000, date(2026, 10, 24))
    first = _request(400_000, date(2026, 10, 25), "IP00-001625")
    second = _request(455_000, date(2026, 10, 25), "IP00-001626")
    (found,) = find_duplicates([manual, first, second])
    assert found.kind is DuplicateKind.SPLIT_1C
    assert found.manual == [manual]
    assert {p.id for p in found.requests} == {first.id, second.id}


def test_запись_не_попадает_в_две_группы():
    payments = [
        _manual(400_000, date(2026, 10, 24)), _manual(455_000, date(2026, 10, 24)),
        _request(855_000, date(2026, 10, 24), "IP00-1"),
        _manual(100_000, date(2026, 10, 24)), _request(100_000, date(2026, 10, 24), "IP00-2"),
    ]
    found = find_duplicates(payments)
    ids = [p.id for suspect in found for p in suspect.manual + suspect.requests]
    assert len(ids) == len(set(ids)) == 5
    assert sorted(_kinds(found), key=str) == sorted(
        [DuplicateKind.SPLIT_LOCAL, DuplicateKind.DOUBLE], key=str)


def test_другой_получатель_отменённые_и_без_даты_не_считаются():
    manual = _manual(800_000, date(2026, 10, 24))
    assert not find_duplicates([manual, _request(800_000, date(2026, 10, 24), recipient="Сафило СНГ ООО")])
    assert not find_duplicates([
        _manual(800_000, date(2026, 10, 24), status=PaymentStatus.CANCELLED),
        _request(800_000, date(2026, 10, 24))])
    assert not find_duplicates([manual, _request(800_000, None)])


def test_два_ручных_платежа_без_заявки_не_дубль():
    assert not find_duplicates([_manual(800_000, date(2026, 10, 24)),
                                _manual(800_000, date(2026, 10, 24))])


def test_ручная_оплаченная_при_неоплаченной_заявке_не_отмечается():
    manual = _manual(800_000, date(2026, 10, 24), status=PaymentStatus.PAID)
    (found,) = find_duplicates([manual, _request(800_000, date(2026, 10, 24))])
    assert found.warning and not found.recommended


def test_порядок_входа_не_меняет_результат():
    payments = [
        _manual(400_000, date(2026, 10, 24)), _manual(455_000, date(2026, 10, 25)),
        _request(855_000, date(2026, 10, 25)),
        _manual(50_000, date(2026, 10, 26), "Сафило СНГ ООО"),
        _request(52_000, date(2026, 10, 26), "IP00-7", "Сафило СНГ ООО"),
    ]

    def shape(found):
        return [(s.kind, sorted(p.id for p in s.manual), sorted(p.id for p in s.requests))
                for s in found]

    assert shape(find_duplicates(payments)) == shape(find_duplicates(reversed(payments)))


# --- через базу ---------------------------------------------------------------

def _save(db, amount: float, pay: date, **fields) -> Payment:
    return store.save_payment(Payment(
        amount=amount, recipient="НеваЛайн ООО", pay_date=pay,
        origin=PaymentOrigin.MANUAL, **fields), db)


def _import(db, tmp_path, *rows: str) -> None:
    service.apply_import(service.analyze_import(_csv(tmp_path, *rows), today=TODAY, db_path=db),
                         today=TODAY, db_path=db, link=False)


def _open_total(db) -> float:
    return sum(p.amount for p in store.list_payments(None, db) if p.counts_to_budget)


def test_сдвиг_даты_после_импорта_находится_и_схлопывается(tmp_path, db):  # noqa: F811
    manual = _save(db, 800_000.0, date(2026, 10, 24), comment="по счёту")
    _import(db, tmp_path, _row("IP00-001625", "20.10.2026", "855 876,00", "НеваЛайн ООО",
                              "25.10.2026"))
    # Дата не совпала — импорт заявку не занял, и деньги посчитаны дважды.
    assert _open_total(db) == pytest.approx(1_655_876.0)
    (found,) = service.find_duplicates(db_path=db)
    assert found.kind is DuplicateKind.DOUBLE and [p.id for p in found.manual] == [manual.id]

    assert service.resolve_duplicates([found], today=TODAY, db_path=db) == (1, 0)
    cancelled = store.get_payment(manual.id, db)
    assert cancelled.status is PaymentStatus.CANCELLED
    assert cancelled.comment.startswith("по счёту\n")
    assert "IP00-001625 от 20.10.2026" in cancelled.comment
    assert _open_total(db) == pytest.approx(855_876.0)
    assert service.find_duplicates(db_path=db) == []
    # Повторная отмена того же найденного ничего не ломает и ничего не считает.
    assert service.resolve_duplicates([found], today=TODAY, db_path=db) == (0, 0)


def test_часть_счёта_после_импорта_находится_с_учётом_занятой_части(tmp_path, db):  # noqa: F811
    left = _save(db, 400_000.0, date(2026, 10, 24))
    taken = _save(db, 455_000.0, date(2026, 10, 25))
    _import(db, tmp_path, _row("IP00-001625", "20.10.2026", "855 000,00", "НеваЛайн ООО",
                              "25.10.2026"))
    # Заявка заняла часть на 25.10, а часть на 24.10 осталась рядом.
    assert store.get_payment(taken.id, db).doc_number == "IP00-001625"
    assert _open_total(db) == pytest.approx(1_255_000.0)
    (found,) = service.find_duplicates(db_path=db)
    assert found.kind is DuplicateKind.SPLIT_LOCAL
    assert [p.id for p in found.manual] == [left.id]
    assert found.inside == pytest.approx(455_000.0) and found.delta == pytest.approx(0.0)

    service.resolve_duplicates([found], today=TODAY, db_path=db)
    assert _open_total(db) == pytest.approx(855_000.0)


def test_общая_база_закрыта_для_не_администратора(db, monkeypatch):  # noqa: F811
    from app.core.payments import transport

    monkeypatch.setattr(transport.session, "token", "t")
    monkeypatch.setattr(transport.session, "base_url", "http://сервер")
    monkeypatch.setattr(transport.session, "is_admin", False)
    with pytest.raises(service.DuplicatesNotAllowed):
        service.find_duplicates(db_path=db)
    with pytest.raises(service.DuplicatesNotAllowed):
        service.resolve_duplicates([], db_path=db)
