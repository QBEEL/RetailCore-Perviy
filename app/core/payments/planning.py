"""Закономерности в истории оплат и предложения по ним.

Работает только на предложение: ничего не создаёт и не меняет. Пользователь
видит строку с обоснованием и решает сам — тихо созданный платёж на несколько
миллионов был бы хуже отсутствия подсказки.

Все пороги подобраны по фактической истории. У шести из десяти крупнейших
получателей медиана промежутка укладывается в 7–14 дней, и разброс у них
небольшой — на таких данных подсказка осмысленна. Там, где разброс велик,
правило молчит: лучше не подсказать, чем подсказать неверно.
"""
from __future__ import annotations

import statistics as stats_module
from collections import defaultdict
from datetime import date, timedelta
from typing import Sequence

from . import analytics
from .models import (
    Payment,
    PaymentStatus,
    Suggestion,
    SuggestionKind,
    SupplierStats,
)
from .recipients import recipient_key

# Сколько оплат нужно, чтобы говорить о ритме.
MIN_PAYMENTS = 4

# Регулярность измеряется долей промежутков, попавших в ±50 % от медианы, а не
# коэффициентом вариации. Причина в данных: у 136 получателей с достаточной
# историей медианный коэффициент вариации равен 0,93, и порог «не больше
# трети» пропускал ровно двоих — при том что платят им явно ритмично. Один
# сдвинутый на месяц платёж раздувает отклонение, но ритма не отменяет.
INTERVAL_TOLERANCE = 0.5
MIN_REGULAR_SHARE = 0.55

# Разброс сумм, при котором сумму можно предложить. Суммы ведут себя ровнее
# промежутков, здесь коэффициент вариации работает.
MAX_AMOUNT_SPREAD = 0.30

# Во сколько раз тишина должна превысить обычный промежуток, чтобы
# предупреждать. Два с половиной — уже явно выбивается из ритма.
SILENT_FACTOR = 2.5

# Выше этого тишина перестаёт быть просрочкой и становится историей: с
# поставщиком просто больше не работают. В базе есть получатели, которым не
# платили свыше четырёх лет, и предупреждать о них — засорять список.
MAX_SILENT_DAYS = 120

# Насколько уверенно поставщик держится одного числа месяца.
DAY_SHARE = 40.0

# Сколько последних оплат берётся для рекомендуемой суммы: старые цены
# неактуальны, а по одной-двум нельзя судить.
RECENT_WINDOW = 6


def suggestions(
    payments: Sequence[Payment],
    *,
    today: date | None = None,
    limit: int = 0,
) -> list[Suggestion]:
    """Предложения по всей истории — сначала срочные, потом регулярные."""
    moment = today or date.today()
    groups = _groups(payments)
    found: list[Suggestion] = []
    for group in groups.values():
        stats = analytics.supplier_history(group[0].recipient, group)
        if (silent := silent_warning(stats, group, moment)) is not None:
            # Нарушенный ритм важнее соблюдённого: два предложения по одному
            # поставщику только запутали бы.
            found.append(silent)
            continue
        if (regular := regular_payment(stats, group, moment)) is not None:
            found.append(regular)
    found.sort(key=lambda item: (not item.urgent, -item.stats.total))
    return found[:limit] if limit else found


def silent_warning(
    stats: SupplierStats,
    payments: Sequence[Payment],
    today: date | None = None,
) -> Suggestion | None:
    """Поставщик давно не оплачивался — при том, что раньше платили регулярно."""
    moment = today or date.today()
    paid = _paid(payments)
    if len(paid) < analytics.MIN_HISTORY or stats.median_interval <= 0:
        return None
    if _has_open(payments):
        # Оплата уже запланирована — предупреждать не о чем.
        return None
    silent = stats.silent_days(moment)
    threshold = stats.median_interval * SILENT_FACTOR
    if not threshold <= silent <= MAX_SILENT_DAYS:
        # Ниже порога — ритм не нарушен, выше потолка — с поставщиком больше
        # не работают, и это не просрочка.
        return None
    reasons = [
        f"последняя оплата {_days(silent)} назад",
        f"обычно каждые {stats.median_interval:.0f} дн",
        f"оплат в истории: {len(paid)}",
    ]
    return Suggestion(
        kind=SuggestionKind.SILENT,
        stats=stats,
        pay_date=_workday(moment),
        amount=recommended_amount(payments) or stats.median_amount,
        reasons=reasons,
    )


def regular_payment(
    stats: SupplierStats,
    payments: Sequence[Payment],
    today: date | None = None,
) -> Suggestion | None:
    """Ритм устойчив — предложить следующую оплату."""
    moment = today or date.today()
    paid = _paid(payments)
    if len(paid) < MIN_PAYMENTS or _has_open(payments):
        return None
    gaps = analytics.intervals(payments)
    share = regular_share(gaps)
    if share < MIN_REGULAR_SHARE or stats.last_pay is None:
        return None
    expected = stats.last_pay + timedelta(days=round(stats.median_interval))
    if expected < moment:
        expected = moment
    reasons = [
        f"каждые {stats.median_interval:.0f} дн",
        f"ритм соблюдён в {share * 100:.0f} % случаев",
        f"оплат: {len(paid)}",
    ]
    if stats.day_share >= DAY_SHARE:
        expected = _nearest_day(expected, stats.common_day)
        reasons.append(f"обычно {stats.common_day}-го числа")
    if amount := recommended_amount(payments):
        reasons.append("сумма стабильна")
    return Suggestion(
        kind=SuggestionKind.REGULAR,
        stats=stats,
        pay_date=_workday(expected),
        amount=amount or stats.median_amount,
        reasons=reasons,
    )


def recommended_amount(payments: Sequence[Payment]) -> float:
    """Рекомендуемая сумма — медиана последних оплат, если они близки.

    При большом разбросе сумма не предлагается: подставленные наугад миллионы
    опаснее пустого поля.
    """
    recent = [p.amount for p in _paid(payments)[-RECENT_WINDOW:]]
    if len(recent) < analytics.MIN_HISTORY:
        return 0.0
    if spread(recent) > MAX_AMOUNT_SPREAD:
        return 0.0
    return float(stats_module.median(recent))


def regular_share(gaps: Sequence[int]) -> float:
    """Доля промежутков, укладывающихся в ±50 % от медианы.

    Устойчива к единичным сдвигам: один платёж, задержанный на месяц, портит
    среднее и стандартное отклонение, но почти не влияет на эту долю.
    """
    if len(gaps) < 2:
        return 0.0
    middle = stats_module.median(gaps)
    if middle <= 0:
        return 0.0
    limit = middle * INTERVAL_TOLERANCE
    return sum(1 for gap in gaps if abs(gap - middle) <= limit) / len(gaps)


def spread(values: Sequence[float]) -> float:
    """Коэффициент вариации: разброс относительно среднего."""
    if len(values) < 2:
        return 0.0
    average = stats_module.fmean(values)
    if average <= 0:
        return 0.0
    return stats_module.pstdev(values) / average


def payment_terms(payments: Sequence[Payment], *, minimum: int = analytics.MIN_HISTORY) -> float:
    """Отсрочка поставщика по истории — медиана «дата платежа − дата заявки».

    Пусто, если истории мало: подставлять отсрочку по одной заявке нельзя.
    """
    values = analytics.terms([p for p in payments if p.status is PaymentStatus.PAID])
    if len(values) < minimum:
        return 0.0
    return float(stats_module.median(values))


def suggest_date(
    terms_days: float,
    *,
    today: date | None = None,
    common_day: int = 0,
) -> date:
    """Дата оплаты по отсрочке: сдвигается с выходного на рабочий день."""
    moment = (today or date.today()) + timedelta(days=int(round(terms_days)))
    if common_day:
        moment = _nearest_day(moment, common_day)
    return _workday(moment)


def _workday(moment: date) -> date:
    """Ближайший рабочий день вперёд.

    В истории на выходные приходится около трёх процентов оплат — подсказывать
    субботу значит подсказывать заведомо неудобную дату.
    """
    while moment.weekday() >= 5:
        moment += timedelta(days=1)
    return moment


def _nearest_day(moment: date, day: int) -> date:
    """Подтягивает дату к привычному числу месяца, не уходя далеко."""
    import calendar

    limit = calendar.monthrange(moment.year, moment.month)[1]
    target = moment.replace(day=min(day, limit))
    if abs((target - moment).days) <= 15:
        return target
    return moment


def _groups(payments: Sequence[Payment]) -> dict[str, list[Payment]]:
    groups: dict[str, list[Payment]] = defaultdict(list)
    for payment in payments:
        if payment.status is PaymentStatus.CANCELLED or not payment.recipient:
            continue
        groups[recipient_key(payment.recipient)].append(payment)
    return groups


def _paid(payments: Sequence[Payment]) -> list[Payment]:
    return sorted(
        (p for p in payments if p.status is PaymentStatus.PAID and p.pay_date is not None),
        key=lambda p: p.pay_date,
    )


def _has_open(payments: Sequence[Payment]) -> bool:
    return any(p.status.open for p in payments)


def _days(count: int) -> str:
    tail = count % 100
    if 11 <= tail <= 14:
        word = "дней"
    elif count % 10 == 1:
        word = "день"
    elif count % 10 in (2, 3, 4):
        word = "дня"
    else:
        word = "дней"
    return f"{count} {word}"
