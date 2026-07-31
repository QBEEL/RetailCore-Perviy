"""Показатели по истории оплат.

Считается по той же выборке, что показана в таблице: фильтр один на весь
модуль, иначе дашборд и таблица разошлись бы в цифрах.

Медиана предпочтена среднему почти везде. Причина в данных: отсрочка по всей
базе имеет среднее 11,5 дня при медиане 6 — несколько заявок с отсрочкой в
десять месяцев тянут среднее вверх и делают его бесполезным для подсказок.
Средние значения тоже считаются: они запрошены и нужны для сводки.
"""
from __future__ import annotations

import statistics as stats_module
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Iterable, Sequence

from .models import (
    Day,
    MONTHS,
    Payment,
    PaymentStatus,
    Period,
    Stats,
    SupplierStats,
    WEEKDAYS,
)
from .recipients import recipient_key
from .store import Filter, list_payments

# Минимум оплат, при котором ритм поставщика считается осмысленным.
MIN_HISTORY = 3


def overview(payments: Sequence[Payment]) -> Stats:
    """Сводка: объёмы, количества, крайние значения, самый загруженный день."""
    result = Stats()
    if not payments:
        return result
    amounts: list[float] = []
    per_day: dict[date, list[Payment]] = defaultdict(list)
    for payment in payments:
        if payment.status is PaymentStatus.CANCELLED:
            continue
        result.total += payment.amount
        result.count += 1
        amounts.append(payment.amount)
        if payment.status is PaymentStatus.PAID:
            result.paid += payment.amount
            result.paid_count += 1
        elif payment.status is PaymentStatus.OVERDUE:
            result.overdue += payment.amount
            result.overdue_count += 1
        else:
            result.planned += payment.amount
            result.planned_count += 1
        if payment.pay_date is not None:
            per_day[payment.pay_date].append(payment)
        if result.biggest is None or payment.amount > result.biggest.amount:
            result.biggest = payment
    if amounts:
        result.minimum = min(amounts)
        result.maximum = max(amounts)
    if per_day:
        busiest = max(per_day.items(), key=lambda item: sum(p.amount for p in item[1]))
        result.busiest_day = Day(day=busiest[0], payments=busiest[1])
    return result


def by_month(payments: Sequence[Payment]) -> list[Period]:
    """Расходы по месяцам, от старых к новым, с приростом к предыдущему."""
    buckets: dict[tuple[int, int], list[Payment]] = defaultdict(list)
    for payment in _dated(payments):
        buckets[(payment.pay_date.year, payment.pay_date.month)].append(payment)
    periods: list[Period] = []
    for (year, month) in sorted(buckets):
        group = buckets[(year, month)]
        periods.append(Period(
            label=f"{MONTHS[month - 1][:3]} {year}",
            start=date(year, month, 1),
            total=sum(p.amount for p in group),
            count=len(group),
        ))
    return _with_previous(periods)


def by_year(payments: Sequence[Payment]) -> list[Period]:
    """Расходы по годам."""
    buckets: dict[int, list[Payment]] = defaultdict(list)
    for payment in _dated(payments):
        buckets[payment.pay_date.year].append(payment)
    periods = [
        Period(
            label=str(year),
            start=date(year, 1, 1),
            total=sum(p.amount for p in buckets[year]),
            count=len(buckets[year]),
        )
        for year in sorted(buckets)
    ]
    return _with_previous(periods)


def by_day(payments: Sequence[Payment], *, days: int = 0) -> list[Period]:
    """Расходы по дням. `days` ограничивает хвостом последних дней выборки."""
    buckets: dict[date, list[Payment]] = defaultdict(list)
    for payment in _dated(payments):
        buckets[payment.pay_date].append(payment)
    if not buckets:
        return []
    moments = sorted(buckets)
    if days:
        limit = moments[-1] - timedelta(days=days - 1)
        moments = [moment for moment in moments if moment >= limit]
    return [
        Period(
            label=f"{moment.day:02d}.{moment.month:02d}",
            start=moment,
            total=sum(p.amount for p in buckets[moment]),
            count=len(buckets[moment]),
        )
        for moment in moments
    ]


def daily_window(
    payments: Sequence[Payment],
    *,
    back: int = 30,
    ahead: int = 14,
    today: date | None = None,
) -> list[tuple[date, float, float, int]]:
    """Непрерывный ряд дней вокруг текущей даты: дата, оплачено, предстоит, число.

    Ряд именно непрерывный, с нулевыми днями. `by_day` возвращает только дни с
    платежами, и на графике это давало обманчивую картину: подпись обещала
    сорок пять дней, а столбцов было семь, потому что отрезок отсчитывался от
    конца данных, а тот уходит в будущее на три месяца.

    Оплаченное и предстоящее разделены: на дашборде важно видеть, где кончается
    факт и начинается план.
    """
    moment = today or date.today()
    start, end = moment - timedelta(days=back), moment + timedelta(days=ahead)
    paid: dict[date, float] = defaultdict(float)
    planned: dict[date, float] = defaultdict(float)
    counts: dict[date, int] = defaultdict(int)
    for payment in _dated(payments):
        if not start <= payment.pay_date <= end:
            continue
        counts[payment.pay_date] += 1
        if payment.status is PaymentStatus.PAID:
            paid[payment.pay_date] += payment.amount
        else:
            planned[payment.pay_date] += payment.amount
    days = (end - start).days + 1
    return [
        (day, paid.get(day, 0.0), planned.get(day, 0.0), counts.get(day, 0))
        for index in range(days)
        if (day := start + timedelta(days=index))
    ]


def busiest_days(payments: Sequence[Payment], *, limit: int = 10) -> list[Day]:
    """Самые загруженные дни — по сумме, а не по количеству платежей."""
    buckets: dict[date, list[Payment]] = defaultdict(list)
    for payment in _dated(payments):
        buckets[payment.pay_date].append(payment)
    days = [Day(day=moment, payments=group) for moment, group in buckets.items()]
    days.sort(key=lambda day: day.total, reverse=True)
    return days[:limit]


def frequent_days(payments: Sequence[Payment]) -> list[tuple[int, int, float]]:
    """Числа месяца по частоте оплат: число, количество, доля в процентах."""
    counter = Counter(p.pay_date.day for p in _dated(payments))
    total = sum(counter.values())
    if not total:
        return []
    return [
        (day, count, count / total * 100.0)
        for day, count in counter.most_common()
    ]


def by_weekday(payments: Sequence[Payment]) -> list[tuple[str, int, float]]:
    """Дни недели: сколько оплат приходится на каждый.

    В истории на субботу и воскресенье приходится около трёх процентов оплат —
    именно поэтому подсказанная дата сдвигается с выходного на рабочий день.
    """
    counter = Counter(p.pay_date.weekday() for p in _dated(payments))
    amounts: dict[int, float] = defaultdict(float)
    for payment in _dated(payments):
        amounts[payment.pay_date.weekday()] += payment.amount
    return [(WEEKDAYS[index], counter.get(index, 0), amounts[index]) for index in range(7)]


def by_supplier(
    payments: Sequence[Payment],
    *,
    limit: int = 0,
    names: dict[int, str] | None = None,
) -> list[SupplierStats]:
    """Рейтинг получателей с их ритмом оплат — от крупных к мелким."""
    groups: dict[str, list[Payment]] = defaultdict(list)
    titles: dict[str, str] = {}
    for payment in payments:
        if payment.status is PaymentStatus.CANCELLED:
            continue
        key = recipient_key(payment.recipient) or f"#{payment.supplier_id}"
        groups[key].append(payment)
        titles.setdefault(key, payment.recipient)
    result = [
        _supplier_stats(titles.get(key, ""), group, names)
        for key, group in groups.items()
    ]
    result.sort(key=lambda item: item.total, reverse=True)
    return result[:limit] if limit else result


def supplier_history(
    recipient: str,
    payments: Sequence[Payment],
    names: dict[int, str] | None = None,
) -> SupplierStats:
    """Показатели одного получателя."""
    key = recipient_key(recipient)
    group = [p for p in payments if recipient_key(p.recipient) == key]
    return _supplier_stats(recipient, group, names)


def intervals(payments: Sequence[Payment]) -> list[int]:
    """Промежутки в днях между последовательными оплатами."""
    moments = sorted({p.pay_date for p in _dated(payments) if p.status is PaymentStatus.PAID})
    return [(moments[i + 1] - moments[i]).days for i in range(len(moments) - 1)]


def average_interval(payments: Sequence[Payment]) -> float:
    """Средний промежуток между оплатами внутри переданной выборки."""
    gaps = intervals(payments)
    return stats_module.fmean(gaps) if gaps else 0.0


def median_interval(payments: Sequence[Payment]) -> float:
    gaps = intervals(payments)
    return float(stats_module.median(gaps)) if gaps else 0.0


def typical_interval(payments: Sequence[Payment], *, min_history: int = MIN_HISTORY) -> float:
    """Типичный промежуток между оплатами одного поставщика.

    По всей базе интервал считать нельзя: платежи идут почти каждый день, и
    общий промежуток вырождается в единицу независимо от того, как платят
    конкретному поставщику. Осмысленная величина — медиана по медианам
    отдельных получателей, у которых история достаточна.
    """
    groups: dict[str, list[Payment]] = defaultdict(list)
    for payment in payments:
        if payment.status is PaymentStatus.PAID:
            groups[recipient_key(payment.recipient)].append(payment)
    medians = [
        value for group in groups.values()
        if len(group) >= min_history and (value := median_interval(group)) > 0
    ]
    return float(stats_module.median(medians)) if medians else 0.0


def terms(payments: Sequence[Payment]) -> list[int]:
    """Отсрочки: сколько дней прошло от заявки до платежа."""
    return [
        (p.pay_date - p.request_date).days
        for p in payments
        if p.pay_date is not None and p.request_date is not None and p.pay_date >= p.request_date
    ]


def median_terms(payments: Sequence[Payment]) -> float:
    """Медианная отсрочка. Медиана, а не среднее: выбросы до 303 дней его ломают."""
    values = terms(payments)
    return float(stats_module.median(values)) if values else 0.0


def month_totals(payments: Sequence[Payment], year: int, month: int) -> tuple[float, float, int]:
    """Оплачено, предстоит и количество за месяц — основа исполнения бюджета."""
    spent = planned = 0.0
    count = 0
    for payment in _dated(payments):
        if payment.pay_date.year != year or payment.pay_date.month != month:
            continue
        if not payment.counts_to_budget:
            continue
        count += 1
        if payment.status is PaymentStatus.PAID:
            spent += payment.amount
        else:
            planned += payment.amount
    return spent, planned, count


def month_history(payments: Sequence[Payment], month: int) -> list[float]:
    """Сколько уходило в этом месяце в прошлые годы — подсказка при вводе бюджета."""
    buckets: dict[int, float] = defaultdict(float)
    for payment in _dated(payments):
        if payment.pay_date.month == month and payment.counts_to_budget:
            buckets[payment.pay_date.year] += payment.amount
    return [buckets[year] for year in sorted(buckets)]


def days_of(payments: Sequence[Payment], year: int, month: int) -> dict[date, Day]:
    """Платежи месяца, разложенные по дням — для сетки календаря."""
    buckets: dict[date, Day] = {}
    for payment in _dated(payments):
        moment = payment.pay_date
        if moment.year != year or moment.month != month:
            continue
        buckets.setdefault(moment, Day(day=moment)).payments.append(payment)
    for day in buckets.values():
        day.payments.sort(key=lambda p: p.amount, reverse=True)
    return buckets


def load(selection: Filter | None = None, db_path: str | None = None) -> list[Payment]:
    """Выборка для расчётов. Отдельная функция — чтобы тесты работали со списком."""
    return list_payments(selection, db_path, order="pay_date, id")


# --- вспомогательное -----------------------------------------------------------

def _dated(payments: Iterable[Payment]) -> list[Payment]:
    """Только платежи с датой: без неё позиция не попадает ни в день, ни в месяц.

    Незавершённые заявки прошлых лет остаются без даты платежа, и включать их
    в расходы месяца нельзя — они исказили бы и бюджет, и динамику.
    """
    return [
        payment for payment in payments
        if payment.pay_date is not None and payment.status is not PaymentStatus.CANCELLED
    ]


def _with_previous(periods: list[Period]) -> list[Period]:
    for index in range(1, len(periods)):
        periods[index].previous = periods[index - 1].total
    return periods


def _supplier_stats(
    recipient: str,
    group: Sequence[Payment],
    names: dict[int, str] | None = None,
) -> SupplierStats:
    amounts = [p.amount for p in group]
    paid = [p for p in group if p.status is PaymentStatus.PAID and p.pay_date is not None]
    moments = sorted(p.pay_date for p in paid)
    supplier_id = next((p.supplier_id for p in group if p.supplier_id), 0)
    result = SupplierStats(
        recipient=(names or {}).get(supplier_id) or recipient,
        supplier_id=supplier_id,
        total=sum(amounts),
        count=len(group),
        minimum=min(amounts) if amounts else 0.0,
        maximum=max(amounts) if amounts else 0.0,
        median_amount=float(stats_module.median(amounts)) if amounts else 0.0,
        first_pay=moments[0] if moments else None,
        last_pay=moments[-1] if moments else None,
        median_interval=median_interval(group),
        median_terms=median_terms(group),
    )
    if days := Counter(moment.day for moment in moments):
        day, count = days.most_common(1)[0]
        result.common_day = day
        result.day_share = count / len(moments) * 100.0
    return result
