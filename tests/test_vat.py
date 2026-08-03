"""Ставки НДС и расчёт налога от суммы."""
from __future__ import annotations

import pytest

from app.core.payments import vat


@pytest.mark.parametrize("percent, amount, expected", [
    (20, 120_000.00, 20_000.00),
    (22, 122_000.00, 22_000.00),
    (10, 110_000.00, 10_000.00),
    (7, 107_000.00, 7_000.00),
    (5, 105_000.00, 5_000.00),
])
def test_налог_внутри_суммы(percent, amount, expected):
    assert vat.of(percent).vat_of(amount) == expected


def test_без_ндс_даёт_ноль():
    assert vat.of(0).vat_of(1_000_000.0) == 0.0


def test_нулевая_сумма():
    assert vat.of(20).vat_of(0.0) == 0.0


def test_отрицательная_сумма_не_даёт_налога():
    assert vat.of(20).vat_of(-100.0) == 0.0


def test_округление_до_копеек():
    # 1000 / 6 = 166,666666... — налог обязан быть с двумя знаками.
    value = vat.of(20).vat_of(1000.0)
    assert value == 166.67
    assert round(value, 2) == value


def test_сумма_без_налога():
    assert vat.of(20).net_of(120_000.0) == 100_000.0
    assert vat.of(0).net_of(120_000.0) == 120_000.0


def test_налог_и_база_дают_исходную_сумму():
    amount = 771_413.35
    rate = vat.of(20)
    assert round(rate.vat_of(amount) + rate.net_of(amount), 2) == amount


# --- распознавание ставки -------------------------------------------------------

@pytest.mark.parametrize("percent", [20, 22, 10, 7, 5])
def test_ставка_узнаётся_по_сумме_и_налогу(percent):
    rate = vat.of(percent)
    amount = 555_555.55
    found = vat.detect(amount, rate.vat_of(amount))
    assert found is not None and found.percent == percent


def test_нулевой_налог_это_без_ндс():
    found = vat.detect(100_000.0, 0.0)
    assert found is not None and found.percent == 0


def test_чужой_налог_не_подгоняется_под_ставку():
    """Иначе сохранение молча переписало бы проставленную руками сумму."""
    assert vat.detect(100_000.0, 17_000.0) is None


def test_расхождение_в_копейку_прощается():
    """1С и приложение округляют одинаково, но последний знак может разойтись."""
    amount = 1_000_000.0
    exact = vat.of(20).vat_of(amount)
    assert vat.detect(amount, exact - 0.01) is not None
    assert vat.detect(amount, exact + 0.01) is not None


def test_расхождение_в_рубль_не_прощается():
    amount = 1_000_000.0
    assert vat.detect(amount, vat.of(20).vat_of(amount) - 1.0) is None


def test_нулевая_сумма_не_опознаётся():
    assert vat.detect(0.0, 0.0) is None


# --- ставка по истории получателя -----------------------------------------------

def test_ставка_берётся_из_свежей_оплаты():
    history = [(105_000.0, 5_000.0), (120_000.0, 20_000.0)]
    assert vat.guess_by_history(history).percent == 5


def test_неопознанные_записи_пропускаются():
    history = [(100_000.0, 17_000.0), (110_000.0, 10_000.0)]
    assert vat.guess_by_history(history).percent == 10


def test_пустая_история_ничего_не_даёт():
    assert vat.guess_by_history([]) is None


def test_история_без_опознаваемых_ставок():
    assert vat.guess_by_history([(100_000.0, 17_000.0)]) is None


# --- набор ставок ---------------------------------------------------------------

def test_в_списке_есть_все_встреченные_в_истории():
    """5 и 7 процентов платят упрощенцы — без них четверть оплат не выбрать."""
    percents = {rate.percent for rate in vat.RATES}
    assert {20, 22, 10, 7, 5, 0} <= percents


def test_ставка_по_умолчанию_самая_частая():
    assert vat.DEFAULT.percent == 20
