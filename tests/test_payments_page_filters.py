"""Вкладка «Оплаты»: новые отборы таблицы и вкладка исполнения плана.

База временная, окно строится по-настоящему, но перезагрузка выборки
подменена: проверяется то, что страница собирает в `Filter` и что показывает
в таблице исполнения, а не поход в базу.
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

from app.core.payments import Payment, PaymentOrigin, PaymentStatus, data, store
from app.core.settings import AppSettings


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(application, monkeypatch, tmp_path):
    from app.ui import payments_page as module

    database = str(tmp_path / "payments.db")
    monkeypatch.setattr(store, "database_path", lambda: database)
    monkeypatch.setattr(data, "online", lambda: False)

    rows = [
        Payment(amount=100_000.0, pay_date=date(2026, 9, 5), recipient="Альфа",
                origin=PaymentOrigin.PLAN, status=PaymentStatus.PAID, paid_flag=True),
        Payment(amount=60_000.0, pay_date=date(2026, 9, 6), recipient="Бета",
                origin=PaymentOrigin.PLAN, status=PaymentStatus.PLANNED),
        Payment(amount=90_000.0, pay_date=date(2026, 9, 7), recipient="Гамма",
                origin=PaymentOrigin.IMPORT, status=PaymentStatus.PAID, paid_flag=True),
    ]
    for payment in rows:
        store.save_payment(payment, database)

    widget = module.PaymentsPage(AppSettings(), lambda text, kind: None)
    widget.rows = store.list_payments(path=database)
    monkeypatch.setattr(widget, "reload", lambda: None)
    widget.year, widget.month = 2026, 9
    return widget


# --- отборы таблицы ------------------------------------------------------------

def test_отбор_по_источнику_уходит_в_условия(page):
    index = page.origin_filter.findData(PaymentOrigin.PLAN.value)
    assert index > 0, "источники должны быть в списке"
    page.origin_filter.setCurrentIndex(index)
    assert page.current_filter().origins == (PaymentOrigin.PLAN,)


def test_без_выбора_источник_не_ограничивает(page):
    assert page.current_filter().origins == ()


def test_точные_даты_уходят_в_условия(page):
    page.date_from.set_value(date(2026, 9, 1))
    page.date_to.set_value(date(2026, 9, 30))
    selection = page.current_filter()
    assert selection.start == date(2026, 9, 1)
    assert selection.end == date(2026, 9, 30)


def test_даты_наоборот_меняются_местами(page):
    """Границы, введённые задом наперёд, — описка, а не запрос пустой выборки."""
    page.date_from.set_value(date(2026, 9, 30))
    page.date_to.set_value(date(2026, 9, 1))
    selection = page.current_filter()
    assert selection.start == date(2026, 9, 1)
    assert selection.end == date(2026, 9, 30)
    assert page.date_from.value() == date(2026, 9, 1)


def test_точные_даты_отменяют_пресет_периода(page):
    page.period_filter.setCurrentIndex(page.period_filter.findData(30))
    page.date_from.set_value(date(2026, 1, 1))
    page._dates_changed()
    assert page.period_filter.isEnabled() is False
    assert page.period_filter.currentIndex() == 0
    assert page.current_filter().start == date(2026, 1, 1)


def test_очистка_дат_возвращает_пресет(page):
    page.date_from.set_value(date(2026, 1, 1))
    page._dates_changed()
    page.date_from.set_value(None)
    page._dates_changed()
    assert page.period_filter.isEnabled() is True


def test_одна_дата_задаёт_только_свою_границу(page):
    page.date_from.set_value(date(2026, 9, 15))
    selection = page.current_filter()
    assert selection.start == date(2026, 9, 15)
    assert selection.end is None


def test_отбор_по_поставщику_уходит_в_условия(page):
    page._fill_filter_lists(store.known_values())
    index = page.supplier_filter.findData("Альфа")
    assert index > 0, "поставщики должны подтягиваться из базы"
    page.supplier_filter.setCurrentIndex(index)
    assert page.current_filter().recipient == "Альфа"


def test_сброс_очищает_новые_отборы(page):
    page.origin_filter.setCurrentIndex(page.origin_filter.findData(PaymentOrigin.PLAN.value))
    page.date_from.set_value(date(2026, 9, 1))
    page.date_to.set_value(date(2026, 9, 30))
    page._dates_changed()
    page.reset_filters()
    selection = page.current_filter()
    assert selection.origins == ()
    assert selection.start is None
    assert selection.end is None
    assert selection.recipient == ""
    assert page.period_filter.isEnabled() is True


def test_сброс_перезагружает_выборку_один_раз(page, monkeypatch):
    """Каждый список подписан на `reload` — сброс не должен звать его шесть раз."""
    calls: list[int] = []
    monkeypatch.setattr(page, "reload", lambda: calls.append(1))
    page.origin_filter.setCurrentIndex(page.origin_filter.findData(PaymentOrigin.PLAN.value))
    calls.clear()
    page.reset_filters()
    assert len(calls) == 1


# --- вкладка исполнения плана ---------------------------------------------------

def test_вкладка_исполнения_плана_есть_на_странице(page):
    titles = [page.tabs.tabText(i) for i in range(page.tabs.count())]
    assert "Исполнение плана" in titles


def test_таблица_исполнения_показывает_план_и_факт(page):
    """Заявка из 1С — такой же план, как строка из Excel: источник роли не играет."""
    page.refresh_plan_fact()
    model = page.plan_fact_table.model_
    rows = {model.item_at(i).recipient: model.item_at(i) for i in range(model.rowCount())}
    assert rows["Альфа"].planned == 100_000
    assert rows["Альфа"].actual == 100_000
    assert rows["Бета"].planned == 60_000
    assert rows["Бета"].actual == 0
    assert rows["Гамма"].planned == 90_000
    assert rows["Гамма"].actual == 90_000


def test_недобор_виден_в_подписи(page):
    page.refresh_plan_fact()
    # Бета намечена и не оплачена; Альфа и Гамма исполнены полностью.
    assert "недобор больше 20 % у поставщиков: 1" in page.plan_fact_hint.text()
    assert "исполнено 76.0 %" in page.plan_fact_hint.text()


def test_смена_месяца_обновляет_заголовок_вкладки(page):
    page.refresh_plan_fact()
    assert "Сентябрь 2026" in page.plan_month_label.text()
    page.shift_month(1)
    assert "Октябрь 2026" in page.plan_month_label.text()


def test_месяц_без_оплат_не_ломает_вкладку(page):
    page.shift_month(3)
    assert page.plan_fact_table.model_.rowCount() == 0
    assert "оплат нет" in page.plan_fact_summary.text()


# --- общая панель отбора --------------------------------------------------------

def test_панель_отбора_стоит_над_вкладками(page):
    """Выборка общая, поэтому фильтры не должны прятаться в одну вкладку."""
    assert page.filters.parent() is page
    assert page.filters is not page.tabs


def test_кнопка_сворачивает_панель(page):
    page.filters_toggle.setChecked(True)
    assert page.filters_body.isVisibleTo(page) is True
    page.filters_toggle.setChecked(False)
    assert page.filters_body.isVisibleTo(page) is False


def test_свёрнутая_панель_запоминается(page):
    page.filters_toggle.setChecked(False)
    assert page.settings.payment_filters_open is False
    page.filters_toggle.setChecked(True)
    assert page.settings.payment_filters_open is True


def test_подпись_говорит_что_отбор_не_задан(page):
    page._refresh_filters_summary()
    assert page.filters_summary.text() == "отбор не задан"


def test_подпись_перечисляет_заданный_отбор(page):
    """Свёрнутая панель обязана выдать себя: иначе неполный календарь
    не отличить от пустого месяца."""
    page.origin_filter.setCurrentIndex(page.origin_filter.findData(PaymentOrigin.PLAN.value))
    page._refresh_filters_summary()
    text = page.filters_summary.text()
    assert "отбор:" in text
    assert "источник: План из Excel" in text


def test_свои_даты_попадают_в_подпись(page):
    page.date_from.set_value(date(2026, 9, 1))
    page._dates_changed()
    assert "свои даты" in page.filters_summary.text()


# --- удаление с календаря -------------------------------------------------------

def test_календарь_удаляет_выбранные_оплаты(page, monkeypatch):
    answer_delete(monkeypatch, yes=True)
    page.show_day(day_of(page, date(2026, 9, 6)))
    page.day_list.selectAll()
    page.delete_from_day()
    assert "Бета" not in [p.recipient for p in store.list_payments()]
    assert len(store.list_payments()) == 2


def test_оплаченное_тоже_удаляется_с_календаря(page, monkeypatch):
    """Перенос оплаченного запрещён, удаление — нет: как и в таблице."""
    answer_delete(monkeypatch, yes=True)
    page.show_day(day_of(page, date(2026, 9, 5)))
    page.day_list.selectAll()
    page.delete_from_day()
    assert "Альфа" not in [p.recipient for p in store.list_payments()]


def test_отказ_от_удаления_ничего_не_трогает(page, monkeypatch):
    answer_delete(monkeypatch, yes=False)
    page.show_day(day_of(page, date(2026, 9, 6)))
    page.day_list.selectAll()
    page.delete_from_day()
    assert len(store.list_payments()) == 3


def test_без_выделения_не_спрашивают_и_не_удаляют(page, monkeypatch):
    asked = answer_delete(monkeypatch, yes=True)
    page.show_day(day_of(page, date(2026, 9, 6)))
    page.day_list.clearSelection()
    page.delete_from_day()
    assert asked == []
    assert len(store.list_payments()) == 3


def test_кнопка_удаления_включается_по_выделению(page):
    page.show_day(day_of(page, date(2026, 9, 6)))
    assert page.day_delete.isEnabled() is False
    page.day_list.selectAll()
    assert page.day_delete.isEnabled() is True
    assert "(1)" in page.day_delete.text()


def test_таблица_и_календарь_удаляют_одним_путём(page, monkeypatch):
    """Правило «что можно удалить» не должно зависеть от вкладки."""
    calls: list[list] = []
    monkeypatch.setattr(page, "delete_payments", lambda items: calls.append(items))
    page.show_day(day_of(page, date(2026, 9, 6)))
    page.day_list.selectAll()
    page.delete_from_day()
    page.delete_selected()
    assert len(calls) == 2


def day_of(page, moment: date):
    """День календаря из текущей выборки страницы."""
    from app.core.payments import analytics

    return analytics.days_of(page.rows, moment.year, moment.month)[moment]


def answer_delete(monkeypatch, *, yes: bool) -> list[int]:
    """Подменяет вопрос перед удалением; возвращает счётчик обращений к нему."""
    from PySide6.QtWidgets import QMessageBox

    from app.ui import payments_page as module

    asked: list[int] = []

    def question(*args, **kwargs):
        asked.append(1)
        return (QMessageBox.StandardButton.Yes if yes
                else QMessageBox.StandardButton.No)

    monkeypatch.setattr(module.QMessageBox, "question", question)
    return asked


# --- выбор даты в календаре ------------------------------------------------------

def test_у_поля_даты_есть_кнопка_календаря(page):
    """Границы периода обычно не помнят числами, а ищут глазами."""
    assert [action.toolTip() for action in page.date_from.actions()] \
        == ["Выбрать дату в календаре"]


def test_выбор_в_календаре_подставляет_дату(page):
    from PySide6.QtCore import QDate

    page.date_from._show_calendar()
    page.date_from._picked(QDate(2026, 9, 3))

    assert page.date_from.value() == date(2026, 9, 3)
    assert page.date_from.text() == "03.09.2026"


def test_выбор_в_календаре_отменяет_пресет_периода(page):
    """Отбор не должен различать, набрали дату руками или отметили мышью:
    подписчики слушают один сигнал, и пресет периода гаснет в обоих случаях."""
    from PySide6.QtCore import QDate

    page.period_filter.setCurrentIndex(2)

    page.date_from._show_calendar()
    page.date_from._picked(QDate(2026, 9, 3))

    assert page.period_filter.currentIndex() == 0
    assert not page.period_filter.isEnabled()


def test_календарь_открывается_на_уже_введённой_дате(page):
    """Иначе человек, уточняющий границу, каждый раз ищет её заново."""
    page.date_to.set_value(date(2026, 3, 14))

    page.date_to._show_calendar()

    assert page.date_to._calendar.selectedDate().toPython() == date(2026, 3, 14)


def test_пустое_поле_открывает_календарь_на_сегодня(page):
    page.date_from.set_value(None)

    page.date_from._show_calendar()

    assert page.date_from._calendar.selectedDate().toPython() == date.today()


def test_повторное_открытие_не_плодит_окна(page):
    """Календарь открывают и закрывают десятки раз за один отбор."""
    page.date_from._show_calendar()
    first = page.date_from._popup

    page.date_from._show_calendar()

    assert page.date_from._popup is first
