"""Ставки НДС и расчёт налога от суммы платежа.

НДС считается «в том числе»: сумма заявки — это то, что уходит поставщику
целиком, а налог сидит внутри неё. Это не предположение, а вывод из истории:
на 6958 оплатах ни одна строка не легла на формулу «налог сверху», и все
опознанные легли на «в том числе».

Набор ставок тоже взят из истории, а не из общих соображений. Кроме привычных
20 и 10 процентов там есть 22 (с 2026 года), а также 5 и 7 — по ним платят
поставщики на упрощённой системе. Если оставить в списке только двадцатку и
десятку, четверти оплат выбирать будет нечего.
"""
from __future__ import annotations

from dataclasses import dataclass

# Копейка. Больше неё расхождение означает, что ставка другая, а не округление:
# и 1С, и здесь налог округляется до копеек, поэтому сойтись они могут только
# точно либо разойтись на последний знак.
TOLERANCE = 0.01


@dataclass(frozen=True, slots=True)
class Rate:
    """Ставка НДС."""

    percent: int
    title: str

    @property
    def zero(self) -> bool:
        return self.percent == 0

    def vat_of(self, amount: float) -> float:
        """Налог внутри суммы. Ставка 0 даёт ноль при любой сумме."""
        if self.percent <= 0 or amount <= 0:
            return 0.0
        return round(amount * self.percent / (100 + self.percent), 2)

    def net_of(self, amount: float) -> float:
        """Сумма без налога — она же база. Показывается рядом для проверки."""
        return round(amount - self.vat_of(amount), 2)


# Порядок — от самой частой к редкой, чтобы нужное было сверху списка.
RATES: tuple[Rate, ...] = (
    Rate(20, "20%"),
    Rate(22, "22%"),
    Rate(10, "10%"),
    Rate(7, "7%"),
    Rate(5, "5%"),
    Rate(0, "Без НДС"),
)

# Ставка по умолчанию для новой оплаты. Двадцать процентов — самая частая в
# истории; при известном получателе её вытесняет ставка из его прошлых оплат.
DEFAULT = RATES[0]

BY_PERCENT: dict[int, Rate] = {rate.percent: rate for rate in RATES}


def of(percent: int) -> Rate | None:
    return BY_PERCENT.get(percent)


def detect(amount: float, vat: float) -> Rate | None:
    """Какой ставке отвечает пара «сумма и налог».

    Возвращает None, если ни одной не отвечает: так бывает у старых записей и
    у оплат, где налог проставлен руками. Подставлять в этом случае ближайшую
    ставку нельзя — она молча переписала бы сумму налога при первом сохранении.
    """
    if amount <= 0:
        return None
    if not vat:
        return BY_PERCENT[0]
    for rate in RATES:
        if rate.zero:
            continue
        # Разница округляется до копеек перед сравнением: вычитание дробных
        # чисел даёт 0.010000000023 там, где на деле ровно копейка, и строгое
        # сравнение с допуском отвергало бы верную ставку.
        if round(abs(rate.vat_of(amount) - vat), 2) <= TOLERANCE:
            return rate
    return None


def guess_by_history(amounts_and_vats: list[tuple[float, float]]) -> Rate | None:
    """Ставка получателя по его прошлым оплатам — от свежих к старым.

    У поставщика ставка меняется редко, и подставить его собственную полезнее,
    чем общую по умолчанию: упрощенец с пятёркой иначе каждый раз требовал бы
    ручной правки.
    """
    for amount, vat in amounts_and_vats:
        if (rate := detect(amount, vat)) is not None:
            return rate
    return None
