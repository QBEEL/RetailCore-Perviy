"""Тесты показателей и автопланирования по истории оплат."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.payments import Budget, Payment, PaymentStatus, analytics, planning
from app.core.payments.models import BudgetUse

TODAY = date(2026, 7, 30)


def payment(
    amount: float,
    pay: date | None,
    recipient: str = "НеваЛайн ООО",
    *,
    status: PaymentStatus = PaymentStatus.PAID,
    request: date | None = None,
) -> Payment:
    return Payment(
        amount=amount,
        pay_date=pay,
        recipient=recipient,
        status=status,
        request_date=request,
        paid_flag=status is PaymentStatus.PAID,
    )


def rhythm(
    start: date,
    step: int,
    count: int,
    amount: float = 100_000.0,
    recipient: str = "НеваЛайн ООО",
) -> list[Payment]:
    """Ровный ряд оплат через равные промежутки."""
    return [
        payment(amount, start + timedelta(days=step * n), recipient)
        for n in range(count)
    ]


# --- сводка -------------------------------------------------------------------

def test_сводка_считает_объёмы_и_крайние():
    rows = [
        payment(100.0, date(2026, 1, 10)),
        payment(300.0, date(2026, 1, 20)),
        payment(200.0, date(2026, 2, 5)),
    ]
    stats = analytics.overview(rows)
    assert stats.count == 3
    assert stats.total == pytest.approx(600.0)
    assert stats.average == pytest.approx(200.0)
    assert stats.minimum == pytest.approx(100.0)
    assert stats.maximum == pytest.approx(300.0)
    assert stats.biggest is not None and stats.biggest.amount == pytest.approx(300.0)


def test_отменённое_не_попадает_в_объём():
    rows = [
        payment(100.0, date(2026, 1, 10)),
        payment(900.0, date(2026, 1, 11), status=PaymentStatus.CANCELLED),
    ]
    stats = analytics.overview(rows)
    assert stats.count == 1 and stats.total == pytest.approx(100.0)


def test_сводка_делит_оплаченное_запланированное_и_просрочку():
    rows = [
        payment(100.0, date(2026, 1, 10)),
        payment(200.0, date(2026, 12, 1), status=PaymentStatus.PLANNED),
        payment(300.0, date(2026, 1, 5), status=PaymentStatus.OVERDUE),
    ]
    stats = analytics.overview(rows)
    assert (stats.paid, stats.planned, stats.overdue) == (100.0, 200.0, 300.0)
    assert stats.overdue_count == 1


def test_самый_загруженный_день_по_сумме_а_не_по_числу_платежей():
    rows = [
        payment(10.0, date(2026, 1, 10)),
        payment(10.0, date(2026, 1, 10)),
        payment(10.0, date(2026, 1, 10)),
        payment(500.0, date(2026, 1, 11)),
    ]
    stats = analytics.overview(rows)
    assert stats.busiest_day is not None
    assert stats.busiest_day.day == date(2026, 1, 11)


def test_пустая_выборка_не_ломает_сводку():
    stats = analytics.overview([])
    assert stats.count == 0 and stats.average == 0.0 and stats.biggest is None


# --- периоды ------------------------------------------------------------------

def test_расходы_по_месяцам_с_приростом():
    rows = [
        payment(100.0, date(2026, 1, 10)),
        payment(150.0, date(2026, 2, 10)),
    ]
    periods = analytics.by_month(rows)
    assert [p.total for p in periods] == [100.0, 150.0]
    assert periods[0].change == 0.0
    assert periods[1].change == pytest.approx(50.0)


def test_расходы_по_годам():
    rows = [payment(100.0, date(2025, 5, 1)), payment(300.0, date(2026, 5, 1))]
    periods = analytics.by_year(rows)
    assert [p.label for p in periods] == ["2025", "2026"]
    assert periods[1].change == pytest.approx(200.0)


def test_платёж_без_даты_не_попадает_ни_в_месяц_ни_в_год():
    """Незавершённая заявка без даты платежа не должна искажать расходы."""
    rows = [payment(100.0, date(2026, 1, 10)), payment(9_000.0, None)]
    assert sum(p.total for p in analytics.by_month(rows)) == pytest.approx(100.0)
    assert sum(p.total for p in analytics.by_year(rows)) == pytest.approx(100.0)


def test_частые_числа_месяца():
    rows = [
        payment(10.0, date(2026, 1, 10)),
        payment(10.0, date(2026, 2, 10)),
        payment(10.0, date(2026, 3, 5)),
    ]
    frequent = analytics.frequent_days(rows)
    assert frequent[0][0] == 10 and frequent[0][1] == 2


def test_дни_недели_считаются_все_семь():
    rows = [payment(10.0, date(2026, 7, 27))]  # понедельник
    week = analytics.by_weekday(rows)
    assert len(week) == 7
    assert week[0][0] == "Пн" and week[0][1] == 1
    assert week[5][1] == 0


# --- ритм поставщика ----------------------------------------------------------

def test_интервал_поставщика_по_медиане():
    rows = rhythm(date(2026, 1, 1), 14, 6)
    assert analytics.median_interval(rows) == pytest.approx(14.0)


def test_типичный_интервал_считается_по_поставщикам_а_не_по_базе():
    """По всей базе платежи идут почти каждый день, и общий интервал вырождается."""
    rows = rhythm(date(2026, 1, 1), 30, 5, recipient="А")
    rows += rhythm(date(2026, 1, 8), 30, 5, recipient="Б")
    rows += rhythm(date(2026, 1, 15), 30, 5, recipient="В")
    # Дни оплат чередуются, поэтому промежуток «по базе» много меньше настоящего.
    assert analytics.median_interval(rows) < 15
    assert analytics.typical_interval(rows) == pytest.approx(30.0)


def test_отсрочка_по_медиане_устойчива_к_выбросу():
    """Среднее по базе — 11,5 дня при медиане 6: несколько выбросов его ломают."""
    rows = [
        payment(100.0, date(2026, 1, 7), request=date(2026, 1, 1)),
        payment(100.0, date(2026, 2, 7), request=date(2026, 2, 1)),
        payment(100.0, date(2026, 3, 7), request=date(2026, 3, 1)),
        payment(100.0, date(2026, 12, 1), request=date(2026, 4, 1)),
    ]
    assert analytics.median_terms(rows) == pytest.approx(6.0)


def test_рейтинг_получателей_по_объёму():
    rows = [
        payment(100.0, date(2026, 1, 1), "Мелкий ООО"),
        payment(900.0, date(2026, 1, 2), "Крупный ООО"),
    ]
    top = analytics.by_supplier(rows)
    assert [s.recipient for s in top] == ["Крупный ООО", "Мелкий ООО"]


def test_получатель_с_разной_формой_имени_это_один_получатель():
    rows = [
        payment(100.0, date(2026, 1, 1), "НеваЛайн ООО"),
        payment(200.0, date(2026, 1, 2), "ООО НеваЛайн"),
    ]
    top = analytics.by_supplier(rows)
    assert len(top) == 1 and top[0].total == pytest.approx(300.0)


# --- бюджет -------------------------------------------------------------------

def test_расход_месяца_делится_на_оплаченное_и_предстоящее():
    rows = [
        payment(100.0, date(2026, 8, 5)),
        payment(200.0, date(2026, 8, 20), status=PaymentStatus.PLANNED),
        payment(900.0, date(2026, 9, 1)),
    ]
    spent, planned, count = analytics.month_totals(rows, 2026, 8)
    assert (spent, planned, count) == (100.0, 200.0, 2)


def test_налоги_и_аренда_не_идут_в_бюджет():
    """Бюджет считается только по оплатам поставщикам — так решено в задании."""
    rows = [payment(100.0, date(2026, 8, 5))]
    rows[0].operation = "Оплата поставщику"
    other = payment(5_000.0, date(2026, 8, 6))
    other.operation = "Перечисление налогов и взносов"
    spent, planned, count = analytics.month_totals([*rows, other], 2026, 8)
    assert spent == pytest.approx(100.0) and count == 1


def test_исполнение_бюджета_и_превышение():
    use = BudgetUse(budget=Budget(year=2026, month=8, amount=1_000.0), spent=600.0, planned=200.0)
    assert use.total == pytest.approx(800.0)
    assert use.left == pytest.approx(200.0)
    assert use.percent == pytest.approx(80.0)
    assert not use.over and use.near(75.0)
    use.planned = 600.0
    assert use.over


def test_бюджет_без_суммы_не_даёт_деления_на_ноль():
    use = BudgetUse(budget=Budget(year=2026, month=8, amount=0.0), spent=500.0)
    assert use.percent == 0.0 and not use.over


def test_история_месяца_по_годам():
    rows = [
        payment(100.0, date(2024, 8, 5)),
        payment(200.0, date(2025, 8, 5)),
        payment(300.0, date(2026, 8, 5)),
        payment(999.0, date(2026, 9, 5)),
    ]
    assert analytics.month_history(rows, 8) == [100.0, 200.0, 300.0]


# --- календарь ----------------------------------------------------------------

def test_дни_месяца_группируют_платежи():
    rows = [
        payment(100.0, date(2026, 8, 5)),
        payment(200.0, date(2026, 8, 5)),
        payment(300.0, date(2026, 8, 6)),
        payment(400.0, date(2026, 9, 6)),
    ]
    days = analytics.days_of(rows, 2026, 8)
    assert set(days) == {date(2026, 8, 5), date(2026, 8, 6)}
    assert days[date(2026, 8, 5)].total == pytest.approx(300.0)
    assert days[date(2026, 8, 5)].count == 2
    # Внутри дня крупные платежи идут первыми.
    assert days[date(2026, 8, 5)].payments[0].amount == pytest.approx(200.0)


def test_выходной_день_помечается():
    days = analytics.days_of([payment(100.0, date(2026, 8, 1))], 2026, 8)
    assert days[date(2026, 8, 1)].weekend


# --- автопланирование ---------------------------------------------------------

def test_ровный_ритм_даёт_предложение():
    rows = rhythm(date(2026, 1, 5), 14, 12)
    stats = analytics.supplier_history("НеваЛайн ООО", rows)
    suggestion = planning.regular_payment(stats, rows, TODAY)
    assert suggestion is not None
    assert suggestion.amount == pytest.approx(100_000.0)
    assert "каждые 14 дн" in suggestion.reason


def test_беспорядочные_оплаты_предложения_не_дают():
    rows = [
        payment(100.0, date(2026, 1, 1)),
        payment(100.0, date(2026, 1, 3)),
        payment(100.0, date(2026, 4, 15)),
        payment(100.0, date(2026, 4, 16)),
        payment(100.0, date(2026, 7, 1)),
    ]
    stats = analytics.supplier_history("НеваЛайн ООО", rows)
    assert planning.regular_payment(stats, rows, TODAY) is None


def test_единичный_сдвиг_ритма_не_отменяет_предложение():
    """Коэффициент вариации на этом ломался: один сдвиг обнулял всю закономерность."""
    rows = rhythm(date(2026, 1, 5), 14, 10)
    rows.append(payment(100_000.0, date(2026, 7, 20)))
    stats = analytics.supplier_history("НеваЛайн ООО", rows)
    assert planning.regular_payment(stats, rows, TODAY) is not None


def test_нестабильная_сумма_не_подставляется():
    rows = [
        payment(amount, date(2026, 1, 5) + timedelta(days=14 * n))
        for n, amount in enumerate([10_000.0, 900_000.0, 25_000.0, 700_000.0, 5_000.0])
    ]
    assert planning.recommended_amount(rows) == 0.0


def test_стабильная_сумма_подставляется():
    rows = rhythm(date(2026, 1, 5), 14, 6, amount=250_000.0)
    assert planning.recommended_amount(rows) == pytest.approx(250_000.0)


def test_давняя_тишина_вызывает_предупреждение():
    rows = rhythm(date(2026, 3, 2), 7, 8)
    stats = analytics.supplier_history("НеваЛайн ООО", rows)
    warning = planning.silent_warning(stats, rows, TODAY)
    assert warning is not None and warning.urgent


def test_совсем_старая_история_предупреждения_не_даёт():
    """Кому не платили четыре года — с тем не работают, это не просрочка."""
    rows = rhythm(date(2022, 1, 10), 7, 8)
    stats = analytics.supplier_history("НеваЛайн ООО", rows)
    assert planning.silent_warning(stats, rows, TODAY) is None


def test_запланированная_оплата_снимает_предупреждение():
    rows = rhythm(date(2026, 3, 2), 7, 8)
    rows.append(payment(100_000.0, date(2026, 8, 10), status=PaymentStatus.PLANNED))
    stats = analytics.supplier_history("НеваЛайн ООО", rows)
    assert planning.silent_warning(stats, rows, TODAY) is None


def test_один_поставщик_не_даёт_двух_предложений():
    rows = rhythm(date(2026, 3, 2), 7, 12)
    found = planning.suggestions(rows, today=TODAY)
    assert len(found) == 1


def test_дата_подсказки_не_попадает_на_выходной():
    """В истории на выходные приходится около трёх процентов оплат."""
    saturday = date(2026, 8, 1)
    assert saturday.weekday() == 5
    assert planning.suggest_date(2, today=date(2026, 7, 30)).weekday() < 5
    assert planning._workday(saturday) == date(2026, 8, 3)


def test_отсрочка_поставщика_по_истории():
    rows = [
        payment(100.0, date(2026, 1, 15), request=date(2026, 1, 1)),
        payment(100.0, date(2026, 2, 15), request=date(2026, 2, 1)),
        payment(100.0, date(2026, 3, 15), request=date(2026, 3, 1)),
    ]
    assert planning.payment_terms(rows) == pytest.approx(14.0)


def test_отсрочка_по_одной_оплате_не_считается():
    rows = [payment(100.0, date(2026, 1, 15), request=date(2026, 1, 1))]
    assert planning.payment_terms(rows) == 0.0


def test_дата_по_отсрочке_учитывает_привычное_число():
    moment = planning.suggest_date(10, today=date(2026, 8, 1), common_day=12)
    assert moment.day == 12


# --- работа с настоящей выгрузкой ---------------------------------------------

DATA = Path(__file__).resolve().parents[1] / "Для планирования"
HISTORY = DATA / "Оплата поставщикам.csv"

needs_files = pytest.mark.skipif(not HISTORY.exists(), reason="нет выгрузки оплат")


@pytest.fixture(scope="module")
def history():
    from app.core.payments import importer

    payments, _ = importer.parse(str(HISTORY), today=TODAY)
    return payments


@needs_files
def test_показатели_настоящей_истории(history):
    stats = analytics.overview(history)
    assert stats.count == 6825
    assert stats.total == pytest.approx(2_415_300_759.0, rel=1e-6)
    assert stats.maximum == pytest.approx(11_719_639.0)


@needs_files
def test_выходные_в_истории_почти_свободны(history):
    week = analytics.by_weekday(history)
    weekend = week[5][1] + week[6][1]
    assert weekend / sum(day[1] for day in week) < 0.05


@needs_files
def test_предложений_немного_и_они_разделены(history):
    from app.core.payments.models import SuggestionKind

    found = planning.suggestions(history, today=TODAY)
    silent = [s for s in found if s.kind is SuggestionKind.SILENT]
    # Без потолка тишины предупреждений было 180 — в основном о поставщиках,
    # с которыми не работают несколько лет.
    assert 0 < len(silent) < 60
    assert all(s.stats.silent_days(TODAY) <= planning.MAX_SILENT_DAYS for s in silent)


# --- ряд по дням для графика --------------------------------------------------

def test_ряд_по_дням_непрерывен():
    """На графике отрезок обещает N дней и обязан содержать ровно N точек."""
    rows = [payment(100.0, date(2026, 7, 20)), payment(200.0, date(2026, 8, 5))]
    window = analytics.daily_window(rows, back=30, ahead=14, today=TODAY)
    assert len(window) == 45
    assert window[0][0] == date(2026, 6, 30)
    assert window[-1][0] == date(2026, 8, 13)
    # Дни без оплат остаются в ряду нулями, иначе даты на оси врут.
    assert sum(1 for _, _, _, count in window if count == 0) == 43


def test_ряд_по_дням_делит_факт_и_план():
    rows = [
        payment(100.0, date(2026, 7, 20)),
        payment(500.0, date(2026, 8, 5), status=PaymentStatus.PLANNED),
    ]
    window = {day: (paid, planned) for day, paid, planned, _ in
              analytics.daily_window(rows, back=30, ahead=14, today=TODAY)}
    assert window[date(2026, 7, 20)] == (100.0, 0.0)
    assert window[date(2026, 8, 5)] == (0.0, 500.0)


def test_ряд_по_дням_не_берёт_данные_за_отрезком():
    """Прежняя версия отсчитывала отрезок от конца данных, а тот уходит в будущее."""
    rows = [payment(100.0, date(2026, 10, 20)), payment(200.0, date(2026, 7, 25))]
    window = analytics.daily_window(rows, back=30, ahead=14, today=TODAY)
    assert sum(count for _, _, _, count in window) == 1
