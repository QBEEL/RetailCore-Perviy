"""Оплата «вместо другого поставщика»: что делать с заменённой оплатой.

Когда одного поставщика оплачивают на место другого, у того остаётся
намеченная оплата, которой не будет. Она либо отменяется целиком, либо
сокращается на сумму новой оплаты. Заменить больше, чем намечено, нельзя:
деньги, которых у заменяемого не было, неоткуда взять.

Модуль ничего не сохраняет — он решает, можно ли, и готовит изменённую копию.
Сохраняет вызывающий, тем же путём, что и любую оплату: общая база и локальная
правятся одинаково.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from enum import Enum
from typing import Sequence

from .models import AMOUNT_EPSILON, Payment, PaymentStatus, money_text
from .recipients import recipient_key


class Mode(str, Enum):
    """Что сделать с заменяемой оплатой."""

    CANCEL = "cancel"
    REDUCE = "reduce"


def candidates(payments: Sequence[Payment], replaced: str, *, own_id: int = 0) -> list[Payment]:
    """Оплаты поставщика, которые ещё можно заменить: ждут денег и идут в бюджет.

    Оплаченная заменена быть не может — деньги уже ушли, отменённая — уже
    не ждёт. Ближайшие по дате — первыми, у оплат без даты порядок по номеру.
    """
    key = recipient_key(replaced)
    found = [p for p in payments
             if p.id != own_id and recipient_key(p.recipient) == key
             and p.status.open and p.counts_to_budget]
    found.sort(key=lambda p: (p.pay_date is None, p.pay_date or date.max, p.id))
    return found


def nearest(options: Sequence[Payment], moment: date | None) -> int:
    """Номер оплаты, которую вероятнее всего и заменяют: ближайшая по дате."""
    if moment is None:
        return 0
    dated = [(abs((p.pay_date - moment).days), index)
             for index, p in enumerate(options) if p.pay_date]
    return min(dated)[1] if dated else 0


def problem(amount: float, target: Payment, mode: Mode) -> str:
    """Почему так заменить нельзя; пустая строка — можно."""
    if amount <= 0:
        return "Укажите сумму новой оплаты"
    if amount > target.amount + AMOUNT_EPSILON:
        return (f"Сумма {money_text(amount)} ₽ больше оплаты «{target.title}» "
                f"({money_text(target.amount)} ₽) — заменить её нельзя")
    if mode is Mode.REDUCE and amount >= target.amount - AMOUNT_EPSILON:
        return ("Сумма равна заменяемой оплате — сокращать нечего, "
                "отмените оплату поставщика")
    return ""


def outcome(amount: float, target: Payment, mode: Mode) -> str:
    """Что станет с заменяемой оплатой — для подсказки в карточке."""
    if mode is Mode.CANCEL:
        return f"Оплата «{target.title}» на {money_text(target.amount)} ₽ будет отменена"
    return (f"Оплата «{target.title}» сократится с {money_text(target.amount)} ₽ "
            f"до {money_text(target.amount - amount)} ₽")


def apply(target: Payment, new: Payment, mode: Mode) -> Payment:
    """Копия заменяемой оплаты после замены. Сумму сверяет `problem`.

    НДС при сокращении уменьшается в той же пропорции, что и сумма: иначе
    ставка оплаты поплыла бы. В комментарий дописывается, чем заменили, — чтобы
    отмену или сокращение потом можно было объяснить.
    """
    reason = problem(new.amount, target, mode)
    if reason:
        raise ValueError(reason)
    when = f" на {new.pay_date:%d.%m.%Y}" if new.pay_date else ""
    note = f"Заменена оплатой «{new.title}» {money_text(new.amount)} ₽{when}"
    comment = f"{target.comment}\n{note}".strip()
    if mode is Mode.CANCEL:
        return replace(target, status=PaymentStatus.CANCELLED, paid_flag=False,
                       comment=comment)
    left = target.amount - new.amount
    return replace(target, amount=left, vat=round(target.vat * left / target.amount, 2),
                   comment=comment)
