"""Выгрузка своей базы оплат в общую.

Администратор может работать на локальной базе при действующем входе: прогнать
у себя выгрузку 1С и присланные планы, посмотреть, что вышло, и только потом
отдать отделу. Этот модуль — вторая половина такой работы.

Выгрузка сливает, а не зеркалит. Записи, которых нет у меня, но есть на
сервере, не трогаются никогда: пока я работал у себя, коллеги работали в общей
базе, и удалить их оплаты «за компанию» было бы худшим из возможных исходов.
Обратное неверно: расхождение решается в пользу локальной записи — выгрузку
делает тот, кто эти данные и готовил.

Опознание записи устроено в три уровня, по тому, откуда она взялась.

Строки из 1С сходятся по паре «номер заявки + дата заявки» — тому же ключу, по
которому работает импорт. План из Excel — по метке плана, поставщику и дате.
У созданных вручную своего ключа нет вовсе, и для них берётся получатель, дата
и ответственный. Сумма в ключ не входит намеренно: исправленная сумма иначе
выглядела бы как новая оплата и легла бы на сервер вторым платежом того же дня,
то есть удвоила бы его в бюджете. Две оплаты одному поставщику в один день от
одного человека по этому ключу неразличимы — они разбираются по порядку: три
моих против одной серверной дают одно обновление и две новых записи. Порядок
здесь произволен, но безобиден: локальные значения получат обе строки.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Sequence

from .models import AMOUNT_EPSILON, Budget, Payment, PaymentOrigin
from .recipients import recipient_key

Progress = Callable[[int, int], None]

# Поля, по которым сравниваются совпавшие записи. Служебные (`id`, `files`,
# `created_at`, `updated_at`) в сравнение не входят: они у двух баз свои и
# всегда разные, а расхождением это не является.
COMPARED: tuple[str, ...] = (
    "pay_date", "amount", "vat", "currency", "supplier_id", "recipient",
    "status", "source_status", "paid_flag", "operation", "over_limit",
    "priority", "edo_state", "responsible", "comment", "origin", "origin_ref",
)

# Насколько подробно называть расхождение в отчёте.
FIELD_TITLES: dict[str, str] = {
    "pay_date": "дата",
    "amount": "сумма",
    "vat": "НДС",
    "currency": "валюта",
    "supplier_id": "карточка поставщика",
    "recipient": "получатель",
    "status": "статус",
    "source_status": "статус 1С",
    "paid_flag": "отметка об оплате",
    "operation": "операция",
    "over_limit": "сверх лимита",
    "priority": "приоритет",
    "edo_state": "состояние ЭДО",
    "responsible": "ответственный",
    "comment": "комментарий",
    "origin": "источник",
    "origin_ref": "метка источника",
}


@dataclass(slots=True)
class Change:
    """Совпавшая пара «моя запись — серверная» и чем они разошлись."""

    local: Payment
    remote: Payment
    fields: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return self.local.title

    @property
    def what(self) -> str:
        return ", ".join(FIELD_TITLES.get(name, name) for name in self.fields)


@dataclass(slots=True)
class SyncReport:
    """Что даст выгрузка. Показывается до записи на сервер."""

    created: list[Payment] = field(default_factory=list)
    updated: list[Change] = field(default_factory=list)
    same: list[Payment] = field(default_factory=list)
    budgets: list[Budget] = field(default_factory=list)
    # Оплаты, которые есть только на сервере. Не трогаются; число нужно, чтобы
    # человек видел, что общая база живёт своей жизнью, а выгрузка её не стёрла.
    only_remote: int = 0
    written: int = 0
    applied: bool = False

    @property
    def changes(self) -> int:
        return len(self.created) + len(self.updated) + len(self.budgets)

    @property
    def total(self) -> float:
        return sum(p.amount for p in self.created) + sum(c.local.amount for c in self.updated)

    @property
    def summary(self) -> str:
        parts = []
        if self.created:
            parts.append(f"новых {len(self.created)}")
        if self.updated:
            parts.append(f"обновится {len(self.updated)}")
        if self.budgets:
            parts.append(f"бюджетов {len(self.budgets)}")
        if self.same:
            parts.append(f"совпадает {len(self.same)}")
        if not parts:
            parts.append("выгружать нечего")
        return " · ".join(parts)

    def by_origin(self) -> list[tuple[str, int, int]]:
        """Сводка по источнику: откуда взялось, сколько всего и сколько новых."""
        totals: dict[PaymentOrigin, list[int]] = {}
        for payment in self.created:
            totals.setdefault(payment.origin, [0, 0])
            totals[payment.origin][0] += 1
            totals[payment.origin][1] += 1
        for change in self.updated:
            totals.setdefault(change.local.origin, [0, 0])
            totals[change.local.origin][0] += 1
        for payment in self.same:
            totals.setdefault(payment.origin, [0, 0])
            totals[payment.origin][0] += 1
        return [(origin.title, counts[0], counts[1])
                for origin, counts in sorted(totals.items(), key=lambda item: item[0].value)]


def key_of(payment: Payment) -> tuple:
    """Ключ, по которому запись узнаётся в другой базе.

    Три уровня — по происхождению записи. Ключ строится на нормализованном
    имени получателя: «НеваЛайн ООО» и «ООО "Невалайн"» — один поставщик, и
    разойтись из-за написания две базы не должны.
    """
    if payment.doc_number:
        moment = payment.request_date.isoformat() if payment.request_date else ""
        return ("1c", payment.doc_number, moment)
    when = payment.pay_date.isoformat() if payment.pay_date else ""
    if payment.origin is PaymentOrigin.PLAN and payment.origin_ref:
        return ("план", payment.origin_ref, recipient_key(payment.recipient), when)
    return ("вручную", recipient_key(payment.recipient), when,
            recipient_key(payment.responsible))


def differences(local: Payment, remote: Payment) -> tuple[str, ...]:
    """Поля, которыми моя запись отличается от серверной."""
    found: list[str] = []
    for name in COMPARED:
        mine, theirs = getattr(local, name), getattr(remote, name)
        if isinstance(mine, float) or isinstance(theirs, float):
            if abs(float(mine or 0.0) - float(theirs or 0.0)) > AMOUNT_EPSILON:
                found.append(name)
        elif name == "recipient":
            if recipient_key(mine) != recipient_key(theirs):
                found.append(name)
        elif mine != theirs:
            found.append(name)
    return tuple(found)


def compare(
    local: Sequence[Payment],
    remote: Sequence[Payment],
    *,
    local_budgets: Sequence[Budget] = (),
    remote_budgets: Sequence[Budget] = (),
    progress: Progress | None = None,
) -> SyncReport:
    """Считает, что даст выгрузка. Ни в одну базу ничего не пишет."""
    report = SyncReport()
    buckets: dict[tuple, list[Payment]] = {}
    for payment in remote:
        buckets.setdefault(key_of(payment), []).append(payment)
    matched = 0

    total = len(local)
    for number, payment in enumerate(local, start=1):
        if progress is not None and number % 200 == 0:
            progress(number, total)
        group = buckets.get(key_of(payment))
        current = group.pop(0) if group else None
        if current is None:
            report.created.append(payment)
            continue
        matched += 1
        if fields := differences(payment, current):
            report.updated.append(Change(local=payment, remote=current, fields=fields))
        else:
            report.same.append(payment)
    report.only_remote = len(remote) - matched

    known = {(b.year, b.month): b for b in remote_budgets}
    for budget in local_budgets:
        if budget.amount <= 0:
            continue
        current = known.get((budget.year, budget.month))
        if current is None or abs(current.amount - budget.amount) > AMOUNT_EPSILON \
                or current.note != budget.note:
            report.budgets.append(budget)
    if progress is not None:
        progress(total, total)
    return report


def pack(payment: Payment) -> dict[str, Any]:
    """Оплата → строка выгрузки. Ключ считается здесь же, чтобы не разойтись."""
    return {
        "doc_number": payment.doc_number,
        "request_date": _iso(payment.request_date),
        "pay_date": _iso(payment.pay_date),
        "amount": float(payment.amount),
        "vat": float(payment.vat),
        "currency": payment.currency,
        "supplier_id": int(payment.supplier_id),
        "recipient": payment.recipient.strip(),
        "recipient_key": recipient_key(payment.recipient),
        "status": payment.status.value,
        "source_status": payment.source_status,
        "paid_flag": bool(payment.paid_flag),
        "operation": payment.operation,
        "over_limit": bool(payment.over_limit),
        "priority": payment.priority,
        "edo_state": payment.edo_state,
        "responsible": payment.responsible,
        "author": payment.author,
        "comment": payment.comment,
        "had_files": bool(payment.had_files),
        "origin": payment.origin.value,
        "origin_ref": payment.origin_ref,
    }


def batches(items: Sequence[Any], size: int) -> Iterable[list[Any]]:
    """Режет выгрузку на части: семь тысяч строк одним запросом не уходят."""
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None
