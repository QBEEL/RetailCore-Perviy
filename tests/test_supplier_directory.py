"""Направления и закрепление поставщиков за менеджерами.

Сеть не трогается: транспорт подменяется, и проверяется то, ради чего вкладка
и переделывалась, — что «кто платил» и «кто ведёт» больше не путаются между
собой, а заявленный поставщик считается своим сразу.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.payments import transport
from app.core.suppliers import directory


@pytest.fixture
def calls(monkeypatch):
    """Перехват запросов к серверу: что ушло и что вернулось."""
    log: list[tuple[str, str, object]] = []
    answers: dict[str, object] = {}

    def get(path, params=None):
        log.append(("GET", path, params))
        return answers.get(path, {})

    def post(path, body=None, params=None):
        log.append(("POST", path, body))
        return answers.get(path, {"changed": 0, "skipped": []})

    def patch(path, body=None):
        log.append(("PATCH", path, body))
        return answers.get(path, [])

    monkeypatch.setattr(transport, "get", get)
    monkeypatch.setattr(transport, "post", post)
    monkeypatch.setattr(transport, "patch", patch)
    # Часть вызовов сперва спрашивает, выполнен ли вход: без общей базы
    # закреплений не существует, и они честно возвращают пустоту.
    monkeypatch.setattr(transport.session, "base_url", "https://retail.test")
    monkeypatch.setattr(transport.session, "token", "тестовый")
    return log, answers


def _entry(**changes) -> dict:
    """Строка списка в том виде, в каком её отдаёт сервер."""
    row = {
        "recipient_key": "superkosmetiks",
        "recipient": "Суперкосметикс ООО",
        "supplier_id": 0,
        "payments": 12,
        "amount": 480000.0,
        "last_pay": "2026-07-14",
        "managers": [],
        "assigned": [],
        "directions": [],
        "unassigned_payers": [],
    }
    row.update(changes)
    return row


def _person(user_id: int, name: str, state: str = "fixed",
            directions: list[str] | None = None) -> dict:
    return {"user_id": user_id, "full_name": name, "state": state,
            "directions": directions or ["beauty"]}


# --- кто платил и кто ведёт -----------------------------------------------------

def test_плативший_не_становится_ведущим(calls):
    """Разовая оплата за коллегу больше не делает человека менеджером.

    Ровно это и было главной поломкой: список менеджеров считался по оплатам,
    поэтому один платёж за отпускника закреплял поставщика навсегда.
    """
    log, answers = calls
    answers["/api/suppliers"] = {"items": [_entry(
        managers=["Баранова Олеся", "Иванов Евгений"],
        assigned=[_person(7, "Иванов Евгений")],
    )], "total": 1, "page": 1, "page_size": 100}

    page = directory.suppliers()
    entry = page.items[0]

    assert entry.managers == ["Баранова Олеся", "Иванов Евгений"]
    assert [p.full_name for p in entry.assigned] == ["Иванов Евгений"]
    assert not entry.mine(0)


def test_у_поставщика_несколько_ведущих(calls):
    """«Суперкосметикс» ведут двое по разным брендам — оба свои."""
    log, answers = calls
    answers["/api/suppliers"] = {"items": [_entry(assigned=[
        _person(7, "Иванов Евгений"),
        _person(9, "Валева Карина"),
    ])], "total": 1, "page": 1, "page_size": 100}

    entry = directory.suppliers().items[0]

    assert entry.mine(7) and entry.mine(9)
    assert not entry.mine(3)


def test_платили_незакреплённые_видны_отдельно(calls):
    """Оплата за коллегу не блокируется, но и не теряется — она предупреждение."""
    log, answers = calls
    answers["/api/suppliers"] = {"items": [_entry(
        managers=["Иванов Евгений", "Мамонова Екатерина"],
        assigned=[_person(7, "Иванов Евгений")],
        unassigned_payers=["Мамонова Екатерина"],
    )], "total": 1, "page": 1, "page_size": 100}

    entry = directory.suppliers().items[0]

    assert entry.unassigned_payers == ["Мамонова Екатерина"]


# --- черновик и фиксация ---------------------------------------------------------

def test_заявленный_поставщик_мой_до_фиксации(calls):
    """Между заявкой и подтверждением человек не должен оставаться без списка."""
    log, answers = calls
    answers["/api/suppliers"] = {"items": [_entry(
        assigned=[_person(9, "Валева Карина", state="draft")])],
        "total": 1, "page": 1, "page_size": 100}

    entry = directory.suppliers().items[0]

    assert entry.mine(9)
    assert entry.has_draft
    assert "на подтверждении" in entry.assigned_title


def test_зафиксированное_показывается_без_пометки(calls):
    log, answers = calls
    answers["/api/suppliers"] = {"items": [_entry(
        assigned=[_person(7, "Иванов Евгений", state="fixed")])],
        "total": 1, "page": 1, "page_size": 100}

    entry = directory.suppliers().items[0]

    assert entry.assigned_title == "Иванов Евгений"
    assert not entry.has_draft


def test_незакреплённый_поставщик_остаётся_в_списке(calls):
    """Пустое закрепление — не изъян, а рабочий список на время разметки."""
    log, answers = calls
    answers["/api/suppliers"] = {"items": [_entry()],
                                 "total": 1, "page": 1, "page_size": 100}

    entry = directory.suppliers().items[0]

    assert entry.assigned == []
    assert entry.assigned_title == ""
    assert entry.recipient == "Суперкосметикс ООО"


# --- отбор ------------------------------------------------------------------------

def test_фильтры_уходят_на_сервер(calls):
    """Отбор считает сервер: на полутора тысячах строк клиенту это не по силам."""
    log, answers = calls
    answers["/api/suppliers"] = {"items": [], "total": 0, "page": 1, "page_size": 100}

    directory.suppliers(search="кос", direction="beauty", manager=7,
                        state="draft", sort="name", order="asc")

    _, path, params = log[0]
    assert path == "/api/suppliers"
    assert params["direction"] == "beauty"
    assert params["manager"] == 7
    assert params["state"] == "draft"
    assert params["sort"] == "name" and params["order"] == "asc"


def test_пустой_фильтр_не_ограничивает_выборку(calls):
    """Пустые условия уходят как None, иначе сервер отобрал бы по пустой строке."""
    log, answers = calls
    answers["/api/suppliers"] = {"items": [], "total": 0, "page": 1, "page_size": 100}

    directory.suppliers()

    _, _, params = log[0]
    assert params["search"] is None
    assert params["direction"] is None
    assert params["manager"] is None
    assert params["state"] is None


def test_страницы_считаются_по_итогу(calls):
    log, answers = calls
    answers["/api/suppliers"] = {"items": [], "total": 250, "page": 2,
                                 "page_size": 100}

    page = directory.suppliers(page=2)

    assert page.total == 250
    assert page.pages == 3


# --- действия ---------------------------------------------------------------------

def test_заявка_уходит_пачкой(calls):
    """Разметка полутора тысяч поставщиков поштучно невыполнима."""
    log, answers = calls
    answers["/api/suppliers/claims"] = {"changed": 2, "skipped": ["чужой"]}

    result = directory.claim(["первый", "второй", "чужой"])

    method, path, body = log[0]
    assert (method, path) == ("POST", "/api/suppliers/claims")
    assert body == {"keys": ["первый", "второй", "чужой"]}
    assert result.changed == 2
    assert result.skipped == ["чужой"]


def test_отзыв_заявки_отдельным_действием(calls):
    log, answers = calls
    directory.withdraw(["первый"])

    method, path, body = log[0]
    assert (method, path) == ("POST", "/api/suppliers/claims/withdraw")
    assert body == {"keys": ["первый"]}


def test_фиксация_может_касаться_одного_менеджера(calls):
    """Админ подтверждает не всё закрепление поставщика, а конкретную заявку."""
    log, answers = calls
    directory.fix(["superkosmetiks"], [9])

    method, path, body = log[0]
    assert (method, path) == ("POST", "/api/suppliers/assignments/fix")
    assert body == {"keys": ["superkosmetiks"], "user_ids": [9]}


def test_ручные_направления_заменяют_расчёт(calls):
    log, answers = calls
    answers["/api/suppliers/superkosmetiks/directions"] = ["beauty"]

    codes = directory.set_directions("superkosmetiks", ["beauty"])

    method, path, body = log[0]
    assert method == "PATCH"
    assert path == "/api/suppliers/superkosmetiks/directions"
    assert body == {"codes": ["beauty"]}
    assert codes == ["beauty"]


# --- оплата не своему поставщику -----------------------------------------------------

def test_оплата_чужому_поставщику_видна_как_предупреждение(calls):
    """Не запрет: подменить коллегу в отпуске — обычное дело."""
    log, answers = calls
    answers["/api/suppliers/conflicts"] = [{
        "recipient_key": "superkosmetiks", "recipient": "Суперкосметикс ООО",
        "payments": 2, "amount": 15000.0, "assigned": ["Валева Карина"]}]

    items = directory.conflicts()

    assert len(items) == 1
    assert items[0].title == "Суперкосметикс ООО — ведёт Валева Карина"


def test_ничей_поставщик_в_предупреждении_назван_ничьим(calls):
    log, answers = calls
    answers["/api/suppliers/conflicts"] = [{
        "recipient_key": "nevalain", "recipient": "НеваЛайн ООО",
        "payments": 1, "amount": 100.0, "assigned": []}]

    assert directory.conflicts()[0].title == "НеваЛайн ООО — ведёт никто"


# --- работа без общей базы ---------------------------------------------------------

def test_без_входа_направлений_нет_и_ошибки_тоже(monkeypatch):
    """Вкладка обязана открываться офлайн: закрепления просто недоступны."""
    monkeypatch.setattr(transport.session, "token", "")

    assert not directory.online()
    assert directory.directions() == []


# --- направления для отбора оплат ---------------------------------------------------

def test_ключи_направлений_собираются_по_всем_направлениям(calls, monkeypatch):
    """Словарь для отбора оплат: только поставщики с направлением, со всех страниц."""
    log, answers = calls
    answers["/api/suppliers/directions"] = [
        {"id": 1, "code": "beauty", "title": "Beauty", "sort_order": 1},
        {"id": 2, "code": "fashion", "title": "Fashion", "sort_order": 2},
    ]
    pages = {
        ("beauty", 1): {"items": [_entry(directions=["beauty"])],
                        "total": 2, "page": 1, "page_size": 500},
        ("beauty", 2): {"items": [_entry(recipient_key="nevalain",
                                         directions=["beauty", "fashion"])],
                        "total": 2, "page": 2, "page_size": 500},
        ("fashion", 1): {"items": [_entry(recipient_key="nevalain",
                                          directions=["beauty", "fashion"])],
                         "total": 1, "page": 1, "page_size": 500},
    }
    original = transport.get

    def get(path, params=None):
        if path == "/api/suppliers":
            log.append(("GET", path, params))
            return pages[(params["direction"], params["page"])]
        return original(path, params)

    monkeypatch.setattr(transport, "get", get)
    # Страница в пятьсот строк, а в тесте всего две: итог «2» при первой
    # странице в одну строку должен заставить прочитать вторую.
    monkeypatch.setattr(directory, "suppliers",
                        lambda **kw: directory._page(get("/api/suppliers", kw)))

    keys = directory.direction_keys()

    assert keys == {"superkosmetiks": ("beauty",),
                    "nevalain": ("beauty", "fashion")}


def test_без_входа_ключей_направлений_нет(monkeypatch):
    monkeypatch.setattr(transport.session, "token", "")
    assert directory.direction_keys() == {}


def test_ключ_с_кириллицей_кодируется_в_адресе(calls):
    """Настоящие ключи — «сафило снг»: пробел и кириллица в пути запроса."""
    log, _ = calls
    directory.set_directions("сафило снг", ["fashion"])
    _, path, _ = log[0]
    assert path == "/api/suppliers/%D1%81%D0%B0%D1%84%D0%B8%D0%BB%D0%BE%20%D1%81%D0%BD%D0%B3/directions"
