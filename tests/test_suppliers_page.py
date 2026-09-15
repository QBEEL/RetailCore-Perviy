"""Вкладка «Поставщики»: отбор, закрепление и работа без общей базы.

Сервер не поднимается: модуль `directory` подменяется, и проверяется то, что
вкладка делает со своей стороны, — какой запрос уходит при смене отбора, что
показывает строка и какие кнопки доступны.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from app.core.payments import transport
from app.core.settings import AppSettings
from app.core.suppliers import directory


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """Общая база, какой её видит вкладка, и журнал ушедших запросов."""
    from app.core.suppliers import store

    monkeypatch.setattr(store, "database_path", lambda: str(tmp_path / "s.db"))

    calls: list[dict] = []
    state = {
        "online": True,
        "entries": [
            _entry("superkosmetiks", "Суперкосметикс ООО", amount=480000,
                   assigned=[(7, "Иванов Евгений", "fixed")],
                   directions=["beauty"], unassigned=["Мамонова Екатерина"]),
            _entry("nevalain", "НеваЛайн ООО", amount=120000,
                   assigned=[(9, "Валева Карина", "draft")],
                   directions=["beauty"]),
            _entry("modahouse", "Мода Хаус ООО", amount=90000),
        ],
    }

    def suppliers(**kwargs):
        calls.append(kwargs)
        return directory.Page(items=list(state["entries"]),
                              total=len(state["entries"]), page=1, page_size=100)

    # Справочники пусты без входа — как и на самом деле: закреплений и
    # направлений вне общей базы не существует. Если подменить их списком,
    # который приходит всегда, проверка перестанет замечать главное — что
    # окно строится раньше входа.
    def directions():
        if not state["online"]:
            return []
        return [directory.Direction(id=1, code="beauty", title="Beauty", sort_order=1),
                directory.Direction(id=2, code="fashion", title="Fashion", sort_order=2)]

    def managers():
        return [(7, "Иванов Евгений"), (9, "Валева Карина")] if state["online"] else []

    monkeypatch.setattr(directory, "online", lambda: state["online"])
    monkeypatch.setattr(directory, "suppliers", suppliers)
    monkeypatch.setattr(directory, "directions", directions)
    monkeypatch.setattr(directory, "managers", managers)

    monkeypatch.setattr(transport.session, "user_id", 7)
    monkeypatch.setattr(transport.session, "full_name", "Иванов Евгений")
    monkeypatch.setattr(transport.session, "is_admin", False)
    monkeypatch.setattr(transport.session, "directions", ("beauty",))
    return calls, state


def _entry(key: str, name: str, amount: float = 0.0,
           assigned: list[tuple[int, str, str]] | None = None,
           directions: list[str] | None = None,
           unassigned: list[str] | None = None) -> directory.Entry:
    return directory.Entry(
        recipient_key=key, recipient=name, payments=5, amount=amount,
        managers=["Иванов Евгений"],
        assigned=[directory.AssignedManager(user_id=uid, full_name=who, state=st)
                  for uid, who, st in assigned or []],
        directions=directions or [],
        unassigned_payers=unassigned or [])


def _page(application):
    """Собранная вкладка с дождавшимися своего часа фоновыми задачами."""
    from app.ui.suppliers_page import SuppliersPage

    page = SuppliersPage(AppSettings(), lambda *_: None)
    _settle(application)
    return page


def _settle(application) -> None:
    """Дожидается фоновых задач и доставки их сигналов."""
    for _ in range(6):
        QThreadPool.globalInstance().waitForDone(3000)
        application.processEvents()


# --- список ---------------------------------------------------------------------

def test_список_показывает_закрепление_и_направление(application, stub):
    page = _page(application)

    assert len(page.rows) == 3
    first = page.rows[0]
    assert first.name == "Суперкосметикс ООО"
    assert first.directions == "Beauty"
    assert first.assigned == "Иванов Евгений"


def test_черновик_помечен_в_списке(application, stub):
    """Заявленное видно как заявленное — иначе оно неотличимо от решённого."""
    page = _page(application)

    draft = page.rows[1]
    assert draft.entry.has_draft
    assert "на подтверждении" in draft.assigned


def test_незакреплённый_остаётся_видимым(application, stub):
    page = _page(application)

    assert page.rows[2].assigned == "—"


# --- отбор ----------------------------------------------------------------------

def test_первый_отбор_свои_и_своё_направление(application, stub):
    """Умолчание: мои поставщики моего направления. Оба — переключаемы."""
    calls, _ = stub
    _page(application)

    assert calls, "запрос за списком не ушёл"
    last = calls[-1]
    assert last["manager"] == 7
    assert last["direction"] == "beauty"


def test_справочники_подтягиваются_после_входа(application, stub):
    """Окно строится до входа в общую базу — направлений тогда ещё нет.

    Это и была поломка: переключатель оставался с одной кнопкой «Все», а в
    списке менеджеров стояла единственная строка. Справочники грузились в
    конструкторе, когда сессии не существует, и больше никто их не перечитывал.
    """
    _, state = stub
    state["online"] = False
    page = _page(application)

    codes = [b.property("code") for b in page.direction_group.buttons()]
    assert codes == [""], "офлайн направлений быть не может"

    # Человек вошёл и открыл вкладку — главное окно зовёт reload.
    state["online"] = True
    page.reload()
    _settle(application)

    codes = sorted(b.property("code") for b in page.direction_group.buttons())
    assert codes == ["", "beauty", "fashion"]
    assert page.manager.count() > 1


def test_повторный_вход_не_задваивает_кнопки(application, stub):
    """Смена учётной записи пересобирает справочник, а не дописывает его."""
    page = _page(application)
    page.reload()
    _settle(application)

    codes = sorted(b.property("code") for b in page.direction_group.buttons())
    assert codes == ["", "beauty", "fashion"]


def test_чужое_направление_доступно_переключателем(application, stub):
    """Направление отбирает список, но ничего не прячет."""
    calls, _ = stub
    page = _page(application)

    for button in page.direction_group.buttons():
        if button.property("code") == "fashion":
            button.click()
            break
    _settle(application)

    assert calls[-1]["direction"] == "fashion"


def test_все_направления_снимают_отбор(application, stub):
    calls, _ = stub
    page = _page(application)

    page.direction_group.buttons()[0].click()
    _settle(application)

    assert calls[-1]["direction"] == ""


def test_сортировка_считается_на_сервере(application, stub):
    """Страница показывает сотню из полутора тысяч: сортировать внутри неё нельзя."""
    calls, _ = stub
    page = _page(application)

    page._sort_by(0)
    _settle(application)
    assert calls[-1]["sort"] == "name" and calls[-1]["order"] == "asc"

    # Повторный клик по той же колонке разворачивает порядок.
    page._sort_by(0)
    _settle(application)
    assert calls[-1]["sort"] == "name" and calls[-1]["order"] == "desc"


def test_сортировка_по_менеджеру_и_направлению(application, stub):
    """Колонки «Ведёт» и «Направление» тоже сортируются, и по возрастанию.

    Имена читают от «А», а не от «Я»: порядок по убыванию для текстовой
    колонки — лишний клик на каждое обращение к списку.
    """
    calls, _ = stub
    page = _page(application)

    page._sort_by(2)
    _settle(application)
    assert calls[-1]["sort"] == "manager"
    assert calls[-1]["order"] == "asc"

    page._sort_by(1)
    _settle(application)
    assert calls[-1]["sort"] == "direction"
    assert calls[-1]["order"] == "asc"

    # Деньги — наоборот, от большего.
    page._sort_by(4)
    _settle(application)
    assert calls[-1]["sort"] == "amount" and calls[-1]["order"] == "desc"


def test_смена_отбора_возвращает_на_первую_страницу(application, stub):
    calls, _ = stub
    page = _page(application)
    page._page_no = 3

    page._state_chosen()
    _settle(application)

    assert calls[-1]["page"] == 1


# --- карточка -------------------------------------------------------------------

def test_оплата_коллеги_показана_предупреждением(application, stub):
    """Не блокируется и не ошибка — но видно, что платил незакреплённый."""
    page = _page(application)

    page.table.selectRow(0)
    _settle(application)

    assert page.warning_label.isVisibleTo(page)
    assert "Мамонова Екатерина" in page.warning_label.text()


def test_у_незакреплённого_предупреждения_нет(application, stub):
    page = _page(application)

    page.table.selectRow(2)
    _settle(application)

    assert not page.warning_label.isVisibleTo(page)
    assert page.assigned_label.text() == "не закреплён ни за кем"


# --- права ----------------------------------------------------------------------

def test_фиксация_скрыта_от_менеджера(application, stub):
    page = _page(application)

    assert not page.fix_button.isVisibleTo(page)


def test_фиксация_доступна_администратору(application, stub, monkeypatch):
    monkeypatch.setattr(transport.session, "is_admin", True)
    page = _page(application)

    page.table.selectRow(1)
    _settle(application)

    assert page.fix_button.isVisibleTo(page)
    assert page.fix_button.isEnabled()


def test_отзыв_доступен_только_на_своём_черновике(application, stub, monkeypatch):
    """Чужую заявку не отзывают, зафиксированное — тем более."""
    monkeypatch.setattr(transport.session, "user_id", 9)
    page = _page(application)

    page.table.selectRow(0)          # чужое, зафиксированное
    _settle(application)
    assert not page.withdraw_button.isEnabled()

    page.table.selectRow(1)          # свой черновик
    _settle(application)
    assert page.withdraw_button.isEnabled()


# --- без общей базы --------------------------------------------------------------

def test_без_сервера_вкладка_открывается_и_закрепление_недоступно(application, stub):
    """Офлайн видны карточки: разбор прайсов лежит в них и работает всегда."""
    _, state = stub
    state["online"] = False

    page = _page(application)

    assert not page.claim_button.isEnabled()
    assert not page.withdraw_button.isEnabled()
