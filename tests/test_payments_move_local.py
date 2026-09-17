"""Перенос обновляет показанное, не перечитывая базу заново.

Раньше каждое перетаскивание в календаре заканчивалось полным чтением: сервер
отдавал всю историю одним ответом — около пяти мегабайт на боевой базе, — и
человек ждал этого после каждой мелкой правки даты. Перенос меняет два поля у
нескольких известных строк, и всё, что показано, пересчитывается из них же.

Здесь проверяется именно это: что чтения не случилось, что показанное сошлось
с записанным и что строка, выпавшая из отбора, с экрана ушла — иначе экономия
превратилась бы в показ выборки, которой на сервере нет.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.core.payments import Payment, PaymentStatus, data, store
from app.core.settings import AppSettings

DAY = date(2026, 9, 10)
TARGET = date(2026, 9, 17)


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(application, monkeypatch, tmp_path):
    """Страница на временной базе с журналом обращений к хранилищу."""
    from app.ui import payments_page as module

    database = str(tmp_path / "payments.db")
    monkeypatch.setattr(store, "database_path", lambda: database)
    monkeypatch.setattr(data, "online", lambda: False)

    for amount, status, name in [
        (10000.0, PaymentStatus.PLANNED, "Альфа"),
        (20000.0, PaymentStatus.PLANNED, "Бета"),
        (30000.0, PaymentStatus.OVERDUE, "Гамма"),
    ]:
        store.save_payment(
            Payment(amount=amount, pay_date=DAY, status=status, recipient=name))

    toasts: list[tuple[str, object]] = []
    widget = module.PaymentsPage(AppSettings(), lambda text, kind: toasts.append((text, kind)))
    widget.toasts = toasts
    widget.rows = store.list_payments()
    widget._total = len(widget.rows)

    # Журнал чтений: перенос не должен трогать ничего из этого. Считаются
    # обращения после построения страницы — она читает базу при создании.
    reads: list[str] = []
    for name in ("list_payments", "count_payments", "known_values",
                 "refresh_overdue", "unlinked_recipients", "budgets"):
        original = getattr(store, name)

        def watched(*args, _name=name, _original=original, **kwargs):
            reads.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(store, name, watched)
    widget.reads = reads
    return widget


def stored(name: str) -> Payment:
    return next(p for p in store.list_payments() if p.recipient == name)


def shown(page, name: str) -> Payment | None:
    return next((p for p in page.rows if p.recipient == name), None)


def ids_of(page, *names: str) -> list[int]:
    return [p.id for p in page.rows if p.recipient in names]


def test_перенос_не_перечитывает_базу(page):
    """То, ради чего всё затевалось: ни одного чтения после записи."""
    page.move_payments(ids_of(page, "Альфа"), TARGET)

    assert page.reads == []


def test_показанное_сходится_с_записанным(page):
    """Строка на экране должна совпасть с тем, что легло в базу, — иначе
    экономия на чтении превращается в показ выдуманных данных."""
    page.move_payments(ids_of(page, "Альфа", "Гамма"), TARGET)

    for name in ("Альфа", "Гамма"):
        assert shown(page, name).pay_date == stored(name).pay_date == TARGET
        assert shown(page, name).status is stored(name).status is PaymentStatus.MOVED
    # Соседняя строка не тронута: переносится ровно выделение.
    assert shown(page, "Бета").pay_date == DAY


def test_сводки_пересчитаны_из_обновлённых_строк(page):
    """Календарь и объём выборки считаются из тех же строк, что и таблица."""
    page.move_payments(ids_of(page, "Альфа"), TARGET)

    assert "показано 3" in page.subtitle.text()
    assert page.stats.total == sum(p.amount for p in page.rows)


def test_выпавшая_из_отбора_строка_уходит_с_экрана(page):
    """При отборе по статусу перенесённое ему больше не отвечает: сервер такую
    строку не вернул бы, и показывать её значило бы врать о выборке."""
    index = page.status_filter.findData(PaymentStatus.PLANNED.value)
    page.status_filter.blockSignals(True)
    page.status_filter.setCurrentIndex(index)
    page.status_filter.blockSignals(False)

    page.move_payments(ids_of(page, "Альфа"), TARGET)
    # Снимок до проверок: `stored` сам читает базу и попал бы в журнал.
    reads = list(page.reads)

    assert shown(page, "Альфа") is None
    assert stored("Альфа").status is PaymentStatus.MOVED
    assert reads == []


def test_попавшая_в_отбор_строка_остаётся(page):
    """Обратный случай: отбор по периоду, перенос внутри него — строка на месте."""
    page.date_from.set_value(date(2026, 9, 1))
    page.date_to.set_value(date(2026, 9, 30))

    page.move_payments(ids_of(page, "Альфа"), TARGET)

    assert shown(page, "Альфа").pay_date == TARGET
    assert page.reads == []


def test_чужие_оплаты_не_уходят_на_сервер(page, monkeypatch):
    """Право считает сервер, но знать заранее, что уйдёт, нужно здесь: иначе
    после отказа непонятно, какие строки правились, а какие нет."""
    alpha = shown(page, "Альфа")
    monkeypatch.setattr(store, "may_edit", lambda payment: payment.id != alpha.id)
    sent: list[list[int]] = []
    original = store.update_many
    monkeypatch.setattr(store, "update_many",
                        lambda ids, *a, **k: (sent.append(list(ids)),
                                              original(ids, *a, **k))[1])

    page.move_payments(ids_of(page, "Альфа", "Бета"), TARGET)

    assert sent == [[shown(page, "Бета").id]]
    assert stored("Альфа").pay_date == DAY
    assert "пропущено чужих: 1" in page.toasts[-1][0]


def test_выделение_целиком_из_чужих_ничего_не_пишет(page, monkeypatch):
    monkeypatch.setattr(store, "may_edit", lambda payment: False)
    monkeypatch.setattr(store, "update_many",
                        lambda *a, **k: pytest.fail("запись чужих оплат"))

    page.move_payments(ids_of(page, "Альфа"), TARGET)

    assert "все выделенные оплаты чужие" in page.toasts[-1][0]


def test_аналитика_не_считается_пока_на_неё_не_смотрят(page, monkeypatch):
    """Рейтинг поставщиков и графики по всей выборке — это больше сотни
    миллисекунд. При перетаскивании открыт календарь, и платить их за экран,
    которого никто не видит, незачем."""
    page._loaded = True
    page.tabs.setCurrentIndex(0)
    built: list[str] = []
    monkeypatch.setattr(page, "refresh_dashboard", lambda: built.append("аналитика"))
    monkeypatch.setattr(page, "refresh_calendar", lambda: built.append("календарь"))

    page.move_payments(ids_of(page, "Альфа"), TARGET)

    assert built == ["календарь"]


def test_отложенная_вкладка_пересобирается_при_переходе(page, monkeypatch):
    """Обратная сторона отсрочки: перейдя на вкладку, человек обязан увидеть
    свежие числа, а не те, что были до переноса."""
    page._loaded = True
    page.tabs.setCurrentIndex(0)
    built: list[str] = []
    monkeypatch.setattr(page, "refresh_dashboard", lambda: built.append("аналитика"))
    page.move_payments(ids_of(page, "Альфа"), TARGET)

    page.tabs.setCurrentIndex(2)

    assert built == ["аналитика"]
    # Второй заход по той же вкладке пересчёта не повторяет: данные уже свежие.
    page.tabs.setCurrentIndex(0)
    page.tabs.setCurrentIndex(2)
    assert built == ["аналитика"]


def test_расхождение_прав_приводит_к_честному_чтению(page, monkeypatch):
    """Сервер записал не то, что мы ждали, — значит показанному верить нельзя,
    и остаётся перечитать, как раньше."""
    # Отправили две строки, записалась одна — такого расхождения быть не должно.
    monkeypatch.setattr(store, "update_many", lambda *a, **k: 1)
    reloaded: list[bool] = []
    monkeypatch.setattr(page, "reload", lambda: reloaded.append(True))

    page.move_payments(ids_of(page, "Альфа", "Бета"), TARGET)

    assert reloaded == [True]
