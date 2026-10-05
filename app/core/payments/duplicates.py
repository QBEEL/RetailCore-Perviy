"""Поиск оплат, посчитанных дважды: ручная запись и заявка 1С на одни деньги.

Оплату сначала вносят в программе, а через день-другой по ней заводят заявку в
1С. Импорт сажает заявку на ручную запись (`importer.adoptions`), но только при
точном совпадении даты платежа и только один к одному. Остаются три случая:

* дата в 1С сдвинута на день-два, а сумма отличается («отклонение по сумме»);
* счёт внесён в программе двумя частями, а в 1С он одной заявкой;
* счёт внесён одной записью, а в 1С разбит на две заявки.

Разбор ничего не пишет и не решает за человека: он возвращает группы
«возможных дублей», а отменяет лишнее только `service.resolve_duplicates` и
только отмеченные в окне администратором.

Лишней всегда считается ручная сторона: заявка пришла из 1С, и её суммы и
статусы — источник истины. Ручная запись при этом не удаляется, а отменяется с
пометкой — комментарий и вложения остаются.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from itertools import combinations
from typing import Iterable, Sequence

from .models import AMOUNT_EPSILON, IMPORT_AUTHOR, Payment, PaymentStatus, money_text
from .recipients import recipient_key

# Насколько могут разойтись даты платежа в программе и в 1С.
WINDOW_DAYS = 3
# При сдвинутой дате заявка и запись считаются одной оплатой, только если суммы
# близки: иначе это скорее два разных счёта одного поставщика.
SHIFT_SHARE = 0.25
# Части счёта должны складываться в сумму заявки почти точно.
SPLIT_SHARE = 0.05
# Счёт делят на две-три части; больше — уже не разбиение, а совпадение сумм.
MAX_PARTS = 3
# Сколько ближайших записей перебирать при поиске частей: перебор подмножеств
# растёт быстро, а у получателя в окне в три дня их единицы.
POOL = 8


class DuplicateKind(str, Enum):
    """Как именно оплата попала в базу дважды."""

    DOUBLE = "double"
    SPLIT_LOCAL = "split_local"
    SPLIT_1C = "split_1c"

    @property
    def title(self) -> str:
        return KIND_TITLES[self]


KIND_TITLES: dict[DuplicateKind, str] = {
    DuplicateKind.DOUBLE: "Внесена и в программе, и в 1С",
    DuplicateKind.SPLIT_LOCAL: "Счёт разбит на части в программе",
    DuplicateKind.SPLIT_1C: "Счёт разбит на заявки в 1С",
}


@dataclass(slots=True)
class Suspect:
    """Группа записей, которые, скорее всего, описывают одни и те же деньги."""

    kind: DuplicateKind
    recipient: str
    manual: list[Payment] = field(default_factory=list)
    requests: list[Payment] = field(default_factory=list)
    # Суммы сходятся или совпала дата — иначе человеку стоит проверить глазами.
    confident: bool = True
    # Часть суммы заявки, которую импорт уже взял у другой ручной записи, заняв
    # её место: заявка 855 000 села на часть 455 000, а часть 400 000 осталась.
    inside: float = 0.0

    @property
    def manual_total(self) -> float:
        return sum(p.amount for p in self.manual)

    @property
    def request_total(self) -> float:
        return sum(p.amount for p in self.requests)

    @property
    def delta(self) -> float:
        """Насколько ручная сторона больше заявок: плюс — в программе внесли больше."""
        return self.manual_total + self.inside - self.request_total

    @property
    def shift_days(self) -> int:
        """Наибольшая разница дат платежа между ручной записью и заявкой."""
        return max((abs((m.pay_date - r.pay_date).days)
                    for m in self.manual for r in self.requests
                    if m.pay_date and r.pay_date), default=0)

    @property
    def warning(self) -> str:
        """Причина не отменять без раздумий: ручная сторона уже оплачена, заявка — нет."""
        if any(m.status is PaymentStatus.PAID for m in self.manual) and not all(
                r.status is PaymentStatus.PAID for r in self.requests):
            return "ручная запись уже отмечена оплаченной, а заявка в 1С — нет"
        return ""

    @property
    def recommended(self) -> bool:
        """Отмечать ли группу по умолчанию."""
        return self.confident and not self.warning

    @property
    def title(self) -> str:
        """Одной строкой: что нашлось и на чём сошлось."""
        manual = " + ".join(money_text(p.amount) for p in self.manual)
        if self.inside:
            manual += f" + {money_text(self.inside)} (уже в заявке)"
        requests = " + ".join(money_text(p.amount) for p in self.requests)
        parts = [self.kind.title, f"ручные {manual} ₽ · заявки 1С {requests} ₽"
                 if len(self.manual) > 1 or len(self.requests) > 1 or self.inside
                 else f"в программе {manual} ₽ · в 1С {requests} ₽"]
        if self.shift_days:
            parts.append(f"даты расходятся на {self.shift_days} дн.")
        return " · ".join(parts)

    def note(self, today: date) -> str:
        """Пометка для отменённой ручной записи."""
        numbers = ", ".join(
            f"{r.doc_number} от {r.request_date:%d.%m.%Y}" if r.request_date else r.doc_number
            for r in self.requests)
        noun = "заявки" if len(self.requests) == 1 else "заявок"
        return f"Дубль {noun} {numbers} — отменено при проверке {today:%d.%m.%Y}"


def find_duplicates(payments: Iterable[Payment]) -> list[Suspect]:
    """Группы возможных дублей среди всех оплат. Ничего не меняет."""
    manual: dict[str, list[Payment]] = {}
    requests: dict[str, list[Payment]] = {}
    for payment in payments:
        if payment.status is PaymentStatus.CANCELLED or payment.pay_date is None:
            continue
        key = recipient_key(payment.recipient)
        if not key:
            continue
        side = requests if payment.doc_number else manual
        side.setdefault(key, []).append(payment)

    found: list[Suspect] = []
    for key in sorted(manual.keys() & requests.keys()):
        found.extend(_match(
            sorted(manual[key], key=lambda p: (p.pay_date, p.id)),
            sorted(requests[key], key=lambda p: (p.pay_date, p.id))))
    found.sort(key=lambda s: (not s.recommended, s.recipient.lower(),
                              min(p.pay_date for p in s.manual + s.requests)))
    return found


def _days(first: Payment, second: Payment) -> int:
    return abs((first.pay_date - second.pay_date).days)


def _near(anchor: Payment, pool: Sequence[Payment]) -> list[Payment]:
    """Записи в окне вокруг даты платежа, ближайшие по дате первыми."""
    inside = [p for p in pool if _days(anchor, p) <= WINDOW_DAYS]
    inside.sort(key=lambda p: (_days(anchor, p), abs(p.amount - anchor.amount), p.id))
    return inside[:POOL]


def _share(first: float, second: float) -> float:
    """Расхождение сумм в долях от большей."""
    top = max(first, second)
    return abs(first - second) / top if top else 0.0


def _inside(request: Payment) -> float:
    """Сколько из суммы заявки пришло с ручной записи, которую она заняла при импорте.

    Занимая запись, заявка запоминает прежнюю сумму в «было». Сумма, правленная
    человеком, там же, но с его именем в «кто»; у выгрузки «кто» пусто или «1С»
    (сервер пометку без имени присылает пустой строкой).
    """
    before = request.amount_before
    if before is None or before >= request.amount - AMOUNT_EPSILON:
        return 0.0
    return before if request.amount_changed_by in ("", IMPORT_AUTHOR) else 0.0


def _best_parts(target: Payment, pool: Sequence[Payment], sizes: range,
                base: float = 0.0) -> list[Payment] | None:
    """Части из пула, дающие сумму цели с точностью `SPLIT_SHARE`; лучшие — ближайшие.

    `base` — то, что в сумме цели уже учтено без этих частей (см. `_inside`).
    """
    best: tuple[tuple[float, int, int], list[Payment]] | None = None
    for size in sizes:
        for parts in combinations(pool, size):
            total = base + sum(p.amount for p in parts)
            if abs(total - target.amount) > target.amount * SPLIT_SHARE + AMOUNT_EPSILON:
                continue
            rank = (round(abs(total - target.amount), 2), size,
                    sum(_days(target, p) for p in parts))
            if best is None or rank < best[0]:
                best = (rank, list(parts))
    return best[1] if best else None


def _match(manual: list[Payment], requests: list[Payment]) -> list[Suspect]:
    """Дубли одного получателя. Порядок шагов важен: сначала точные суммы."""
    free_manual = {p.id: p for p in manual}
    free_request = {p.id: p for p in requests}
    found: list[Suspect] = []

    def take(kind: DuplicateKind, own: list[Payment], theirs: list[Payment],
             confident: bool, inside: float = 0.0) -> None:
        for payment in own:
            free_manual.pop(payment.id, None)
        for payment in theirs:
            free_request.pop(payment.id, None)
        found.append(Suspect(kind, (theirs or own)[0].recipient,
                             manual=sorted(own, key=lambda p: (p.pay_date, p.id)),
                             requests=sorted(theirs, key=lambda p: (p.pay_date, p.id)),
                             confident=confident, inside=inside))

    # 1. Суммы сходятся: одна запись, либо две-три части в сумме дают заявку.
    # Если заявка уже заняла одну часть при импорте, оставшиеся добирают разницу.
    for request in sorted(requests, key=lambda p: (-p.amount, p.pay_date, p.id)):
        if request.id not in free_request:
            continue
        inside = _inside(request)
        parts = _best_parts(request, _near(request, list(free_manual.values())),
                            range(1, MAX_PARTS + 1), base=inside)
        if parts:
            kind = (DuplicateKind.DOUBLE if len(parts) == 1 and not inside
                    else DuplicateKind.SPLIT_LOCAL)
            take(kind, parts, [request], True, inside)

    # 2. Заявки разбиты в 1С: две-три заявки в сумме дают ручную запись. Раньше
    # шага 3, иначе запись на 855 000 села бы на одну из заявок по 455 000.
    for payment in sorted(free_manual.values(), key=lambda p: (-p.amount, p.pay_date, p.id)):
        if payment.id not in free_manual:
            continue
        parts = _best_parts(payment, _near(payment, list(free_request.values())),
                            range(2, MAX_PARTS + 1))
        if parts:
            take(DuplicateKind.SPLIT_1C, [payment], parts, True)

    # 3. Остальное — по ближайшей дате: та же дата или близкая сумма.
    for payment in sorted(free_manual.values(), key=lambda p: (p.pay_date, p.id)):
        if payment.id not in free_manual:
            continue
        for request in _near(payment, list(free_request.values())):
            close = _share(payment.amount, request.amount) <= SHIFT_SHARE
            same_day = _days(payment, request) == 0
            if close or same_day:
                take(DuplicateKind.DOUBLE, [payment], [request], same_day and close)
                break
    return found
