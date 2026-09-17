"""Исполнение плана: намеченное и оплаченное по поставщику за месяц.

Планом считается всё намеченное на месяц независимо от источника: счёт, набитый
в 1С на конкретное число, запланирован так же, как строка из присланного Excel.
Оплата не заводит новую запись, а меняет статус существующей, поэтому факт —
всегда часть плана. Отсюда главное, что проверяется: оплаченное попадает в обе
суммы и даёт исполнение 100 %, а не расхождение.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.payments import (
    PLAN_DEVIATION_LIMIT,
    Payment,
    PaymentOrigin,
    PaymentStatus,
    PlanFact,
    analytics,
)

YEAR, MONTH = 2026, 9


def payment(
    amount: float,
    day: int,
    recipient: str = "НеваЛайн ООО",
    *,
    origin: PaymentOrigin = PaymentOrigin.IMPORT,
    status: PaymentStatus = PaymentStatus.PAID,
    operation: str = "Оплата поставщику",
) -> Payment:
    return Payment(
        amount=amount,
        pay_date=date(YEAR, MONTH, day),
        recipient=recipient,
        origin=origin,
        status=status,
        operation=operation,
        paid_flag=status is PaymentStatus.PAID,
    )


# --- что попадает в план --------------------------------------------------------

def test_заявка_из_1с_это_уже_план():
    """Счёт, набитый в 1С и стоящий на число, запланирован не меньше, чем Excel."""
    rows = analytics.plan_vs_fact(
        [payment(100_000, 5, status=PaymentStatus.PLANNED)], YEAR, MONTH)
    assert rows[0].planned == 100_000
    assert rows[0].actual == 0
    assert rows[0].left == 100_000


def test_источник_на_состав_плана_не_влияет():
    rows = analytics.plan_vs_fact([
        payment(100_000, 5, "Excel", origin=PaymentOrigin.PLAN,
                status=PaymentStatus.PLANNED),
        payment(100_000, 5, "Вручную", origin=PaymentOrigin.MANUAL,
                status=PaymentStatus.PLANNED),
        payment(100_000, 5, "Заказ", origin=PaymentOrigin.ORDER,
                status=PaymentStatus.PLANNED),
        payment(100_000, 5, "Переоценка", origin=PaymentOrigin.PRICING,
                status=PaymentStatus.PLANNED),
        payment(100_000, 5, "Из 1С", origin=PaymentOrigin.IMPORT,
                status=PaymentStatus.PLANNED),
    ], YEAR, MONTH)
    assert len(rows) == 5
    assert {row.planned for row in rows} == {100_000}


def test_оплаченное_идёт_и_в_план_и_в_факт():
    rows = analytics.plan_vs_fact([payment(100_000, 5)], YEAR, MONTH)
    assert rows[0].planned == 100_000
    assert rows[0].actual == 100_000
    assert rows[0].left == 0
    assert rows[0].done_share == 100.0
    assert rows[0].off_plan is False


def test_просроченное_остаётся_в_плане_и_в_недоборе():
    rows = analytics.plan_vs_fact(
        [payment(100_000, 5, status=PaymentStatus.OVERDUE)], YEAR, MONTH)
    assert rows[0].planned == 100_000
    assert rows[0].left == 100_000


def test_факт_не_может_превысить_план():
    """Оплата меняет статус записи, а не добавляет сумму сверх намеченного."""
    rows = analytics.plan_vs_fact([
        payment(100_000, 5),
        payment(50_000, 6, status=PaymentStatus.PLANNED),
    ], YEAR, MONTH)
    assert rows[0].planned == 150_000
    assert rows[0].actual == 100_000
    assert rows[0].actual <= rows[0].planned


def test_отменённое_не_попадает_ни_в_план_ни_в_факт():
    rows = analytics.plan_vs_fact([
        payment(100_000, 5, status=PaymentStatus.PLANNED),
        payment(400_000, 6, status=PaymentStatus.CANCELLED),
    ], YEAR, MONTH)
    assert rows[0].planned == 100_000


def test_налоги_и_аренда_не_участвуют_в_сравнении():
    """Недобор должен объясняться работой менеджера, а не арендой."""
    rent = payment(300_000, 8, "Арендодатель", operation="Аренда")
    rows = analytics.plan_vs_fact(
        [payment(100_000, 5, status=PaymentStatus.PLANNED), rent], YEAR, MONTH)
    assert [row.recipient for row in rows] == ["НеваЛайн ООО"]


# --- отбор месяца и сведение получателей ----------------------------------------

def test_чужой_месяц_не_попадает_в_расчёт():
    other = Payment(amount=500_000, pay_date=date(YEAR, MONTH - 1, 5),
                    recipient="НеваЛайн ООО")
    rows = analytics.plan_vs_fact(
        [payment(100_000, 5, status=PaymentStatus.PLANNED), other], YEAR, MONTH)
    assert rows[0].planned == 100_000


def test_платёж_без_даты_не_попадает_в_месяц():
    nowhere = Payment(amount=500_000, pay_date=None, recipient="НеваЛайн ООО")
    rows = analytics.plan_vs_fact(
        [payment(100_000, 5, status=PaymentStatus.PLANNED), nowhere], YEAR, MONTH)
    assert rows[0].planned == 100_000


def test_поставщик_с_разной_формой_имени_это_одна_строка():
    rows = analytics.plan_vs_fact([
        payment(100_000, 5, "НеваЛайн ООО", status=PaymentStatus.PLANNED),
        payment(90_000, 7, 'ООО "Невалайн"'),
    ], YEAR, MONTH)
    assert len(rows) == 1
    assert rows[0].planned == 190_000
    assert rows[0].actual == 90_000


def test_порядок_по_величине_плана():
    """Наверх — крупные планы: недобор по ним стоит дороже всего."""
    rows = analytics.plan_vs_fact([
        payment(1_000_000, 5, "Крупный", status=PaymentStatus.PLANNED),
        payment(10_000, 6, "Мелкий", status=PaymentStatus.PLANNED),
        payment(500_000, 7, "Средний", status=PaymentStatus.PLANNED),
    ], YEAR, MONTH)
    assert [row.recipient for row in rows] == ["Крупный", "Средний", "Мелкий"]


def test_строка_без_плана_и_факта_не_показывается():
    zero = payment(0.0, 5, status=PaymentStatus.PLANNED)
    assert analytics.plan_vs_fact([zero], YEAR, MONTH) == []


# --- итоги ----------------------------------------------------------------------

def test_итоги_считают_план_факт_и_число_выбившихся():
    rows = analytics.plan_vs_fact([
        payment(100_000, 5, "Оплачен"),
        payment(200_000, 6, "Не оплачен", status=PaymentStatus.PLANNED),
    ], YEAR, MONTH)
    planned, actual, off = analytics.plan_totals(rows)
    assert planned == 300_000
    assert actual == 100_000
    assert off == 1


def test_пустая_выборка_не_ломает_расчёт():
    assert analytics.plan_vs_fact([], YEAR, MONTH) == []
    assert analytics.plan_totals([]) == (0, 0, 0)


# --- исполнение -----------------------------------------------------------------

def test_исполнение_в_процентах_и_остаток():
    row = PlanFact(planned=100_000, actual=70_000)
    assert row.done_share == 70.0
    assert row.left == 30_000


def test_недобор_ровно_на_пороге_не_считается_выбившимся():
    """Порог — «недобрали больше 20 %», ровно двадцать остаётся в норме."""
    row = PlanFact(planned=100_000, actual=100_000 * (100.0 - PLAN_DEVIATION_LIMIT) / 100)
    assert row.done_share == 100.0 - PLAN_DEVIATION_LIMIT
    assert row.off_plan is False


def test_недобор_чуть_больше_порога_выбивается():
    row = PlanFact(planned=100_000, actual=79_000)
    assert row.off_plan is True


def test_ничего_не_оплачено_это_недобор():
    row = PlanFact(planned=100_000, actual=0.0)
    assert row.done_share == 0.0
    assert row.left == 100_000
    assert row.off_plan is True


def test_план_исполнен_полностью():
    row = PlanFact(planned=100_000, actual=100_000)
    assert row.done_share == 100.0
    assert row.left == 0
    assert row.off_plan is False


def test_пустая_строка_не_делится_на_ноль_и_не_выбивается():
    row = PlanFact()
    assert row.done_share is None
    assert row.left == 0
    assert row.off_plan is False
    assert row.empty is True


def test_имя_по_умолчанию_когда_получателя_нет():
    assert PlanFact().title == "без получателя"
