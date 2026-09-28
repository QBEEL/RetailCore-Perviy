"""Направление оплаты: Beauty, Fashion или остальные.

Правило проверяется на готовых строках: словарь направлений поставщиков
подставляется руками, сеть и база не нужны.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.payments import Payment
from app.core.payments.directions import (
    NO_DIRECTION,
    RESPONSIBLE_DIRECTIONS,
    by_direction,
    direction_of,
)

SUPPLIERS = {
    "суперкосметикс": ("beauty",),
    "невалайн": ("beauty", "fashion"),
    "фурла": ("fashion",),
}


def _payment(recipient: str, responsible: str = "Когай Анна", amount: float = 1.0,
             operation: str = "Оплата поставщику") -> Payment:
    return Payment(recipient=recipient, responsible=responsible, amount=amount,
                   operation=operation)


def test_поставщик_даёт_направление():
    assert direction_of(_payment("Суперкосметикс ООО"), SUPPLIERS) == "beauty"
    assert direction_of(_payment("ООО Фурла"), SUPPLIERS) == "fashion"


def test_ответственный_fashion_сильнее_поставщика():
    """Платёж Барановой поставщику Beauty — всё равно деньги Fashion."""
    payment = _payment("Суперкосметикс ООО", responsible="Баранова Олеся")
    assert direction_of(payment, SUPPLIERS) == "fashion"


def test_сотрудницы_fashion_в_правиле():
    assert RESPONSIBLE_DIRECTIONS["Баранова Олеся"] == "fashion"
    assert RESPONSIBLE_DIRECTIONS["Мамонова Екатерина"] == "fashion"


def test_лёвкина_fashion_в_обоих_написаниях():
    """1С выгружает её как «Л?вкина» — буква теряется при перекодировке."""
    for name in ("Л?вкина София", "Лёвкина София"):
        assert direction_of(_payment("Суперкосметикс ООО", responsible=name),
                            SUPPLIERS) == "fashion"


def test_поставщик_двух_направлений_относится_к_первому():
    """Делить оплату пополам нечем — берётся первое в порядке отдела."""
    assert direction_of(_payment("НеваЛайн ООО"), SUPPLIERS) == "beauty"


def test_без_направления_оплата_остальная():
    assert direction_of(_payment("Ростелеком ПАО"), SUPPLIERS) == NO_DIRECTION
    assert direction_of(_payment(""), SUPPLIERS) == NO_DIRECTION


def test_направления_не_пересекаются_и_складываются_в_итог():
    """Каждая оплата ровно в одном отборе: три суммы дают итог выборки."""
    rows = [
        _payment("Суперкосметикс ООО", amount=100),
        _payment("Суперкосметикс ООО", responsible="Мамонова Екатерина", amount=40),
        _payment("ООО Фурла", amount=70),
        _payment("Ростелеком ПАО", amount=5),
    ]
    parts = {code: by_direction(rows, code, SUPPLIERS)
             for code in ("beauty", "fashion", NO_DIRECTION)}
    assert [p.amount for p in parts["beauty"]] == [100]
    assert sorted(p.amount for p in parts["fashion"]) == [40, 70]
    assert [p.amount for p in parts[NO_DIRECTION]] == [5]
    assert sum(len(v) for v in parts.values()) == len(rows)


def test_пустое_направление_не_отбирает():
    rows = [_payment("Суперкосметикс ООО"), _payment("Ростелеком ПАО")]
    assert by_direction(rows, "", SUPPLIERS) == rows


# --- статьи расхода -------------------------------------------------------------------

EXPENSE = frozenset({"marketing", "taxes", "rent"})
WITH_EXPENSE = {
    **SUPPLIERS,
    "арендодатель": ("rent",),
    "агентство": ("beauty", "marketing"),
}


def test_расход_поставщика_важнее_ответственного():
    """Аренду магазина Fashion оформила Баранова — это всё равно аренда."""
    payment = _payment("Арендодатель ООО", responsible="Баранова Олеся")
    assert direction_of(payment, WITH_EXPENSE, EXPENSE) == "rent"


def test_расход_важнее_отдела_у_того_же_поставщика():
    assert direction_of(_payment("Агентство ООО"), WITH_EXPENSE, EXPENSE) == "marketing"


def test_налоги_и_аренда_по_операции_1с():
    tax = _payment("УФК по Приморскому краю", operation="Перечисление налогов и взносов")
    lease = _payment("Европлан ПАО", responsible="Баранова Олеся",
                     operation="Оплата арендодателю")
    assert direction_of(tax, WITH_EXPENSE, EXPENSE) == "taxes"
    assert direction_of(lease, WITH_EXPENSE, EXPENSE) == "rent"


def test_операция_без_направления_на_сервере_не_уводит_из_остальных():
    """Сервер без «Налогов» — оплата остаётся там, где её видно."""
    tax = _payment("УФК по Приморскому краю", operation="Перечисление налогов и взносов")
    assert direction_of(tax, WITH_EXPENSE, frozenset()) == NO_DIRECTION


def test_с_расходами_суммы_всё_равно_складываются():
    rows = [
        _payment("Суперкосметикс ООО", amount=100),
        _payment("Арендодатель ООО", responsible="Баранова Олеся", amount=30),
        _payment("УФК", operation="Перечисление налогов и взносов", amount=20),
        _payment("ООО Фурла", amount=70),
        _payment("Ростелеком ПАО", amount=5),
    ]
    codes = ("beauty", "fashion", "marketing", "taxes", "rent", NO_DIRECTION)
    parts = {code: by_direction(rows, code, WITH_EXPENSE, EXPENSE) for code in codes}
    assert sum(len(v) for v in parts.values()) == len(rows)
    assert [p.amount for p in parts["rent"]] == [30]
    assert [p.amount for p in parts["taxes"]] == [20]
