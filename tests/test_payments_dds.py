"""Статья ДДС оплаты: справочник, умолчание по направлению, хранение и выбор.

Сервер не поднимается: вместо него подставляется `transport`. Проверяется то,
что решает приложение, — что предложить, что сохранить и что не затирать.
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

from app.core.payments import Payment, PaymentOrigin, dds, remote, store, sync, transport
from app.core.settings import AppSettings

BEAUTY = "Оплата поставщику (бьюти)"
FASHION = "Оплата поставщику (фэшн)"


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def database(monkeypatch, tmp_path):
    path = str(tmp_path / "payments.db")
    monkeypatch.setattr(store, "database_path", lambda: path)
    return path


@pytest.fixture
def server(monkeypatch):
    """Вошедший пользователь и перехват запросов к серверу."""
    log: list[tuple[str, str, object]] = []
    answers: dict[str, object] = {}

    def reply(method, path, body=None):
        log.append((method, path, body))
        answer = answers.get(path)
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else {}

    monkeypatch.setattr(transport, "get", lambda path, params=None: reply("GET", path))
    monkeypatch.setattr(transport, "post", lambda path, body=None, params=None: reply("POST", path, body))
    monkeypatch.setattr(transport, "patch", lambda path, body=None: reply("PATCH", path, body))
    monkeypatch.setattr(transport.session, "base_url", "https://retail.test")
    monkeypatch.setattr(transport.session, "token", "тестовый")
    return log, answers


# --- справочник ----------------------------------------------------------------

def test_встроенный_список_из_файла_без_дублей_и_пробелов():
    items = dds.BUILTIN_ITEMS

    assert len(items) == 115
    assert len({item.casefold() for item in items}) == len(items)
    assert all(item == dds.clean(item) and item for item in items)
    assert BEAUTY in items and FASHION in items


def test_миграция_сервера_содержит_весь_встроенный_список():
    """Сервер начинает с того же перечня, что и приложение без сервера."""
    sql = (Path(__file__).resolve().parents[1]
           / "server/db/migrations/009_dds_items.sql").read_text(encoding="utf-8")

    for item in dds.BUILTIN_ITEMS:
        assert f"('{item}')" in sql


def test_без_входа_список_встроенный(monkeypatch):
    monkeypatch.setattr(transport.session, "token", "")

    assert dds.items() == list(dds.BUILTIN_ITEMS)


def test_со_входом_список_берётся_с_сервера(server):
    _, answers = server
    answers["/api/dds-items"] = [{"id": 1, "title": "Аренда"},
                                 {"id": 2, "title": "Новая статья"}]

    assert dds.items() == ["Аренда", "Новая статья"]


@pytest.mark.parametrize("failure", [transport.ServerError("Not Found", 404),
                                     transport.OfflineError("нет связи")])
def test_сервер_без_справочника_не_оставляет_без_выбора(server, failure):
    _, answers = server
    answers["/api/dds-items"] = failure

    assert dds.items() == list(dds.BUILTIN_ITEMS)


def test_добавление_отправляет_очищенное_название(server):
    log, answers = server
    answers["/api/dds-items"] = {"id": 116, "title": "Новая статья"}

    assert dds.add("  Новая   статья ") == "Новая статья"
    assert log == [("POST", "/api/dds-items", {"title": "Новая статья"})]


# --- умолчание -----------------------------------------------------------------

def test_умолчание_по_направлению():
    assert dds.default_item(("beauty",)) == BEAUTY
    assert dds.default_item(("fashion",)) == FASHION


def test_оба_отдела_дают_первый():
    assert dds.default_item(("beauty", "fashion")) == BEAUTY
    assert dds.default_item(("fashion", "beauty")) == FASHION


def test_без_направления_умолчания_нет():
    assert dds.default_item(()) == ""
    assert dds.default_item(("marketing",)) == ""


def test_умолчание_берётся_из_сессии(monkeypatch):
    monkeypatch.setattr(transport.session, "directions", ("fashion",))

    assert dds.default_item() == FASHION


# --- хранение ------------------------------------------------------------------

def test_локальная_база_хранит_статью(database):
    saved = store.save_payment(Payment(amount=100.0, recipient="Альфа",
                                       pay_date=date(2026, 10, 5), dds_item="Аренда"))

    assert store.get_payment(saved.id).dds_item == "Аренда"


def test_локальная_база_старой_версии_получает_колонку(database):
    import sqlite3

    from app.core.payments import schema

    connection = sqlite3.connect(database)
    schema._step_1(connection)
    schema._step_2(connection)
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()

    # Любое открытие базы доводит схему до актуальной.
    saved = store.save_payment(Payment(amount=1.0, recipient="Альфа", dds_item="Клининг"))

    assert store.get_payment(saved.id).dds_item == "Клининг"


def test_повторный_импорт_не_стирает_статью(database):
    """Выгрузка 1С статьи не содержит — проставленная человеком остаётся."""
    old = Payment(amount=100.0, recipient="Альфа", doc_number="IP00-1",
                  request_date=date(2026, 9, 1), origin=PaymentOrigin.IMPORT,
                  dds_item="Аренда")
    saved = store.save_payment(old)

    fresh = Payment(amount=150.0, recipient="Альфа", doc_number="IP00-1",
                    request_date=date(2026, 9, 1), origin=PaymentOrigin.IMPORT)
    store.apply_import([], [(saved.id, fresh)])

    row = store.get_payment(saved.id)
    assert row.amount == 150.0
    assert row.dds_item == "Аренда"


def _server_row(**fields) -> dict:
    row = {"id": 7, "doc_number": "", "request_date": None, "pay_date": "2026-10-05",
           "amount": 100, "vat": 0, "currency": "руб.", "supplier_id": 0,
           "recipient": "Альфа", "status": "planned", "source_status": "",
           "paid_flag": False, "operation": "", "over_limit": False, "priority": "",
           "edo_state": "", "responsible": "", "author": "", "comment": "",
           "had_files": False, "origin": "manual", "origin_ref": "",
           "created_at": "2026-10-05T10:00:00", "updated_at": "2026-10-05T10:00:00"}
    row.update(fields)
    return row


def test_сервер_получает_статью_при_создании_и_правке(server):
    log, answers = server
    row = _server_row(dds_item=BEAUTY)
    answers["/api/payments"] = row
    answers["/api/payments/7"] = row

    created = remote.save_payment(Payment(amount=100.0, recipient="Альфа",
                                          pay_date=date(2026, 10, 5), dds_item=BEAUTY))
    remote.save_payment(created)

    assert log[0][2]["dds_item"] == BEAUTY
    assert log[1][2]["dds_item"] == BEAUTY
    assert created.dds_item == BEAUTY


def test_ответ_старого_сервера_без_статьи_читается():
    assert remote._payment(_server_row()).dds_item == ""


# --- выгрузка локальной базы ---------------------------------------------------

def test_выгрузка_передаёт_статью():
    packed = sync.pack(Payment(amount=1.0, recipient="А", dds_item="Аренда"))

    assert packed["dds_item"] == "Аренда"


def test_пустая_статья_у_меня_не_расхождение():
    """Оплаты из 1С локально статьи не имеют, а на сервере её проставили."""
    mine = Payment(amount=1.0, recipient="А")
    theirs = Payment(amount=1.0, recipient="А", dds_item="Аренда")

    assert "dds_item" not in sync.differences(mine, theirs)


def test_моя_статья_против_серверной_расхождение():
    mine = Payment(amount=1.0, recipient="А", dds_item="Клининг")
    theirs = Payment(amount=1.0, recipient="А", dds_item="Аренда")

    assert "dds_item" in sync.differences(mine, theirs)


# --- выбор с поиском -----------------------------------------------------------

def test_поиск_ищет_слова_в_любом_порядке(application):
    from app.ui.widgets.inputs import SearchSelect

    box = SearchSelect()
    box.set_items(list(dds.BUILTIN_ITEMS))

    def found(text: str) -> list[str]:
        box._words.set_text(text)
        return [box._words.index(row, 0).data() for row in range(box._words.rowCount())]

    assert found("поставщику бьюти") == [BEAUTY]
    assert found("БЬЮТИ поставщику") == [BEAUTY]
    assert {BEAUTY, FASHION} <= set(found("оплата поставщику"))
    assert found("абракадабра") == []
    assert found("") == list(dds.BUILTIN_ITEMS)
    assert "Ремонт текущий, работы по содер-ю помещений" in found("ремонт текущий")


def test_выбор_отдаёт_название_из_списка(application):
    from app.ui.widgets.inputs import SearchSelect

    box = SearchSelect()
    box.set_items(["Аренда", "Клининг"])
    assert box.value() == ""

    box.set_value("клининг")
    assert box.value() == "Клининг"

    box.lineEdit().setText("Клинин")
    assert box.value() == "" and box.has_unmatched_text()


def test_статья_которой_нет_в_списке_у_открытой_оплаты_остаётся(application):
    from app.ui.widgets.inputs import SearchSelect

    box = SearchSelect()
    box.set_items(["Аренда"], current="Старая статья")

    assert box.value() == "Старая статья"


# --- карточка оплаты -----------------------------------------------------------

@pytest.fixture
def dialog_for(application, database, monkeypatch):
    from app.core.payments import data
    from app.ui.widgets import payment_dialogs
    from app.ui.widgets.payment_dialogs import PaymentDialog

    monkeypatch.setattr(data, "online", lambda: False)
    # История по поставщику грузится в потоке и читает базу уже после теста.
    monkeypatch.setattr(payment_dialogs, "run_task", lambda *args, **kwargs: None)

    def make(payment: Payment, directions: tuple[str, ...] = ()):
        monkeypatch.setattr(transport.session, "directions", directions)
        return PaymentDialog(payment, recipients=["Альфа"], responsible=[],
                             operations=[], dds_items=["Аренда", BEAUTY, FASHION])
    return make


def test_новая_оплата_менеджера_beauty_получает_статью_beauty(dialog_for):
    dialog = dialog_for(Payment(), ("beauty",))

    assert dialog.dds_item.value() == BEAUTY
    assert dialog.result_payment().dds_item == BEAUTY


def test_новая_оплата_менеджера_fashion_получает_статью_fashion(dialog_for):
    assert dialog_for(Payment(), ("fashion",)).dds_item.value() == FASHION


def test_без_направления_статья_не_подставляется(dialog_for):
    assert dialog_for(Payment(), ()).dds_item.value() == ""


def test_открытая_оплата_без_статьи_остаётся_без_неё(dialog_for):
    """Умолчание — для новых: старую оплату открытием не меняют."""
    dialog = dialog_for(Payment(id=5, amount=1.0, recipient="Альфа"), ("beauty",))

    assert dialog.dds_item.value() == ""


def test_открытая_оплата_показывает_свою_статью(dialog_for):
    dialog = dialog_for(Payment(id=5, amount=1.0, recipient="Альфа", dds_item="Аренда"),
                        ("beauty",))

    assert dialog.dds_item.value() == "Аренда"


def test_статья_не_из_списка_не_даёт_сохранить(dialog_for):
    dialog = dialog_for(Payment(amount=10.0, recipient="Альфа", pay_date=date(2026, 10, 5)),
                        ("beauty",))
    dialog.dds_item.lineEdit().setText("Придуманная статья")

    dialog._accept()

    assert dialog.result() != dialog.DialogCode.Accepted
    assert "нет в списке" in dialog.error.text()


def test_сохранение_с_выбранной_статьёй(dialog_for):
    dialog = dialog_for(Payment(amount=10.0, recipient="Альфа", pay_date=date(2026, 10, 5)),
                        ("beauty",))
    dialog.dds_item.set_value("Аренда")

    dialog._accept()

    assert dialog.result() == dialog.DialogCode.Accepted
    assert dialog.result_payment().dds_item == "Аренда"


# --- администрирование ---------------------------------------------------------

@pytest.fixture
def admin_page(application, server):
    from app.core.settings import AppSettings
    from app.ui import admin_page as module

    toasts: list[str] = []
    page = module.AdminPage(AppSettings(), lambda text, kind: toasts.append(text))
    page.toasts = toasts
    page._apply(([], [], [], [], [], module.admin.Scope(),
                 [dds.DdsItem("Аренда", id=1), dds.DdsItem("Клининг", id=2),
                  dds.DdsItem("Услуги связи", id=3)]))
    return page


def test_список_статей_показан_и_ищется(admin_page):
    assert admin_page.dds_table.model_.rowCount() == 3
    assert "статей: 3" in admin_page.dds_hint.text()

    admin_page.dds_search.setText("связи")

    assert admin_page.dds_table.proxy.rowCount() == 1
    assert "показано 1 из 3" in admin_page.dds_hint.text()


def test_повтор_статьи_отклоняется_до_сервера(admin_page, server):
    log, _ = server
    admin_page.dds_new.setText("  клининг ")

    admin_page.add_dds_item()

    assert log == []
    assert "уже есть" in admin_page.toasts[-1]


def test_пустое_название_не_уходит_на_сервер(admin_page, server):
    log, _ = server
    admin_page.dds_new.setText("   ")

    admin_page.add_dds_item()

    assert log == []


def test_новая_статья_уходит_на_сервер(admin_page, server, monkeypatch):
    from app.ui import admin_page as module

    log, answers = server
    answers["/api/dds-items"] = {"id": 116, "title": "Охрана труда"}
    # Задача выполняется сразу, а не в потоке: важно, что ушло и что показано.
    monkeypatch.setattr(module, "run_task",
                        lambda fn, *a, on_result=None, on_error=None, **k: on_result(fn(*a)))
    monkeypatch.setattr(admin_page, "reload", lambda: None)
    admin_page.dds_new.setText("Охрана труда")

    admin_page.add_dds_item()

    assert log == [("POST", "/api/dds-items", {"title": "Охрана труда"})]
    assert admin_page.dds_new.text() == ""
    assert "добавлена" in admin_page.toasts[-1]


# --- цвет и метка статьи -------------------------------------------------------

PINK = "#EC4899"


@pytest.fixture
def marked(server):
    """Справочник с розовым «Маркетингом» — как его задал администратор."""
    _, answers = server
    answers["/api/dds-items"] = [
        {"id": 1, "title": "Аренда", "color": "", "note": ""},
        {"id": 2, "title": "Маркетинг (ГПН)", "color": PINK, "note": "Маркетинг"},
        {"id": 3, "title": "Маркетинг (Адвент)", "color": PINK, "note": "Маркетинг"},
    ]
    dds.catalog()
    yield
    dds._marks.clear()


def test_каталог_запоминает_только_отмеченные(marked):
    assert dds.mark_of("Маркетинг (ГПН)").color == PINK
    assert dds.mark_of("Маркетинг (ГПН)").note == "Маркетинг"
    assert dds.mark_of("Аренда") is None
    assert dds.mark_of("") is None


def test_сервер_без_цветов_читается(server):
    """Ответ сервера до обновления — без цвета и метки."""
    _, answers = server
    answers["/api/dds-items"] = [{"id": 1, "title": "Аренда"}]

    assert dds.catalog() == [dds.DdsItem("Аренда", id=1)]
    assert dds.mark_of("Аренда") is None


def test_встроенный_список_стирает_старые_пометки(monkeypatch, marked):
    monkeypatch.setattr(transport.session, "token", "")

    dds.catalog()

    assert dds.mark_of("Маркетинг (ГПН)") is None


def test_цвет_и_метка_уходят_одним_запросом(server):
    log, answers = server
    answers["/api/dds-items"] = [{"id": 2, "title": "А"}, {"id": 3, "title": "Б"}]

    assert dds.set_mark([2, 3], PINK, "  Маркетинг ") == 2
    assert log == [("PATCH", "/api/dds-items",
                    {"ids": [2, 3], "color": PINK, "note": "Маркетинг"})]


def test_цвета_для_выбора_корректны():
    from PySide6.QtGui import QColor

    assert dds.COLORS[0] == ("Розовый", PINK)
    assert all(QColor(code).isValid() for _, code in dds.COLORS)
    assert len({code for _, code in dds.COLORS}) == len(dds.COLORS)


def test_таблица_оплат_красит_строку_кружком_и_меткой(application, marked):
    from PySide6.QtCore import Qt

    from app.ui import payments_page as module
    from app.ui.widgets import marks
    from app.ui.widgets.table import ObjectTableModel

    columns = module.PaymentsPage._columns(None)
    model = ObjectTableModel(columns)
    model.set_row_tint(module._tint)
    pink = Payment(amount=1.0, recipient="Фирма", dds_item="Маркетинг (ГПН)")
    plain = Payment(amount=1.0, recipient="Фирма", dds_item="Аренда")
    model.set_items([pink, plain])
    supplier = next(i for i, c in enumerate(columns) if c.title == "Поставщик")
    note = next(i for i, c in enumerate(columns) if c.title == "Заметка")

    assert model.data(model.index(0, supplier), Qt.ItemDataRole.DecorationRole) is not None
    assert model.data(model.index(0, note), Qt.ItemDataRole.DisplayRole) == "Маркетинг"
    assert model.data(model.index(0, 0), Qt.ItemDataRole.BackgroundRole).name() == PINK.lower()
    # Без кружка, но с таким же свободным местом: названия стоят по одной линии.
    blank = model.data(model.index(1, supplier), Qt.ItemDataRole.DecorationRole)
    assert blank.cacheKey() == marks.blank_icon().cacheKey()
    assert blank.cacheKey() != model.data(model.index(0, supplier),
                                          Qt.ItemDataRole.DecorationRole).cacheKey()
    assert model.data(model.index(1, note), Qt.ItemDataRole.DisplayRole) == ""
    assert model.data(model.index(1, 0), Qt.ItemDataRole.BackgroundRole) is None


def test_диалог_цвета_показывает_общее_и_возвращает_выбор(application):
    from app.ui.widgets.dds_dialog import DdsMarkDialog

    both = [dds.DdsItem("А", id=1, color=PINK, note="Маркетинг"),
            dds.DdsItem("Б", id=2, color=PINK, note="Маркетинг")]
    dialog = DdsMarkDialog(both)
    assert dialog.result_mark() == (PINK, "Маркетинг")

    mixed = DdsMarkDialog([dds.DdsItem("А", id=1, color=PINK, note="М"),
                           dds.DdsItem("Б", id=2)])
    assert mixed.result_mark() == ("", "")

    mixed.color.setCurrentIndex(mixed.color.findData("#2563EB"))
    mixed.note.setText("  Синие   дела ")
    assert mixed.result_mark() == ("#2563EB", "Синие дела")


def test_админка_красит_выбранные_статьи(admin_page, server, monkeypatch):
    from app.ui import admin_page as module

    log, answers = server
    answers["/api/dds-items"] = [{"id": 1, "title": "А"}, {"id": 2, "title": "Б"}]
    monkeypatch.setattr(module, "run_task",
                        lambda fn, *a, on_result=None, on_error=None, **k: on_result(fn(*a)))
    monkeypatch.setattr(module.DdsMarkDialog, "exec", lambda self: module.QDialog.DialogCode.Accepted)
    monkeypatch.setattr(module.DdsMarkDialog, "result_mark", lambda self: (PINK, "Маркетинг"))
    monkeypatch.setattr(admin_page, "reload", lambda: None)
    admin_page.dds_table.selectAll()

    admin_page.mark_dds_items()

    assert log[-1][0:2] == ("PATCH", "/api/dds-items")
    assert log[-1][2]["color"] == PINK and sorted(log[-1][2]["ids"]) == [1, 2, 3]
    assert "заданы" in admin_page.toasts[-1]


def test_админка_без_выбора_ничего_не_шлёт(admin_page, server):
    log, _ = server
    admin_page.dds_table.clearSelection()

    admin_page.mark_dds_items()

    assert log == []
    assert "Выберите" in admin_page.toasts[-1]


def test_без_окрашенных_статей_место_под_кружок_не_занимается(application):
    from app.ui import payments_page as module

    dds._marks.clear()

    assert module._dot(Payment(amount=1.0, recipient="А", dds_item="Аренда")) is None


@pytest.mark.parametrize("code", [404, 405])
def test_старый_сервер_отвечает_понятно(server, code):
    _, answers = server
    answers["/api/dds-items"] = transport.ServerError("Method Not Allowed", code)

    with pytest.raises(transport.ServerError, match="не обновлена"):
        dds.set_mark([1], PINK, "Маркетинг")


def _cell(payments):
    from app.core.payments import Day
    from app.ui.widgets.calendar_grid import DayCell

    day = date(2026, 10, 23)
    cell = DayCell()
    cell.show_day(day, Day(day=day, payments=payments))
    return cell


def test_на_клетке_дня_точка_цвета_статьи(application, marked):
    cell = _cell([Payment(amount=1.0, recipient="А", dds_item="Маркетинг (ГПН)"),
                  Payment(amount=1.0, recipient="Б", dds_item="Маркетинг (Адвент)"),
                  Payment(amount=1.0, recipient="В", dds_item="Аренда")])

    shown = [dot for dot in cell.mark_dots if not dot.isHidden()]

    # Два розовых оплаты — одна точка, а не две.
    assert len(shown) == 1
    assert PINK.lower() in shown[0].styleSheet().lower()
    assert "Маркетинг: 2" in cell.toolTip()


def test_без_окрашенных_статей_точек_нет(application, marked):
    cell = _cell([Payment(amount=1.0, recipient="В", dds_item="Аренда")])

    assert all(dot.isHidden() for dot in cell.mark_dots)


def test_точки_одного_размера_с_красной_и_оранжевой(application, marked):
    cell = _cell([Payment(amount=1.0, recipient="А", dds_item="Маркетинг (ГПН)")])

    assert cell.mark_dots[0].size() == cell.alert.size() == cell.changed.size()


# --- фильтр по статье ----------------------------------------------------------

@pytest.fixture
def three(database):
    for name, item in (("Альфа", "Аренда"), ("Бета", "Клининг"), ("Гамма", "")):
        store.save_payment(Payment(amount=10.0, recipient=name, dds_item=item,
                                   pay_date=date(2026, 10, 5)))


def _names(rows):
    return sorted(row.recipient for row in rows)


def test_отбор_по_статье_в_локальной_базе(three):
    assert _names(store.list_payments(store.Filter(dds_item="Аренда"))) == ["Альфа"]


def test_отбор_без_статьи_находит_пустые(three):
    assert _names(store.list_payments(store.Filter(dds_item=dds.NO_ITEM))) == ["Гамма"]


def test_без_отбора_статья_не_ограничивает(three):
    assert len(store.list_payments(store.Filter())) == 3
    assert not store.Filter().active
    assert store.Filter(dds_item="Аренда").active


def test_отбор_по_статье_уходит_на_сервер(server, monkeypatch):
    seen = {}

    def get(path, params=None):
        seen.update(params or {})
        return []

    monkeypatch.setattr(transport, "get", get)
    remote.list_payments(store.Filter(dds_item="Аренда"))

    assert seen["dds_item"] == "Аренда"


def test_старый_сервер_не_выдаёт_всё_за_отобранное(monkeypatch):
    """Сервер без отбора по статье игнорирует параметр и отдаёт всё."""
    rows = [_server_row(id=1, dds_item="Аренда"), _server_row(id=2, dds_item="Клининг"),
            _server_row(id=3, dds_item="")]
    monkeypatch.setattr(transport, "get", lambda path, params=None: rows)
    monkeypatch.setattr(transport.session, "token", "т")
    monkeypatch.setattr(transport.session, "base_url", "https://x")

    assert [p.id for p in remote.list_payments(store.Filter(dds_item="Аренда"))] == [1]
    assert [p.id for p in remote.list_payments(store.Filter(dds_item=dds.NO_ITEM))] == [3]
    assert len(remote.list_payments(store.Filter())) == 3


@pytest.fixture
def filter_page(application, monkeypatch, tmp_path):
    from app.core.payments import data
    from app.ui import payments_page as module

    monkeypatch.setattr(store, "database_path", lambda: str(tmp_path / "p.db"))
    monkeypatch.setattr(data, "online", lambda: False)
    widget = module.PaymentsPage(AppSettings(), lambda text, kind: None)
    monkeypatch.setattr(widget, "reload", lambda: None)
    widget._fill_filter_lists({"dds_items": ["Аренда", "Клининг", BEAUTY]})
    return widget



def test_фильтр_статей_наполнен_и_ищет_по_словам(filter_page):
    box = filter_page.dds_filter

    assert [box.itemText(i) for i in range(box.count())][:3] == [
        "Все статьи ДДС", "Без статьи", "Аренда"]
    box._words.set_text("поставщику бьюти")
    assert [box._words.index(r, 0).data() for r in range(box._words.rowCount())] == [BEAUTY]


def test_выбор_статьи_уходит_в_отбор(filter_page):
    box = filter_page.dds_filter
    assert filter_page.current_filter().dds_item == ""

    box.setCurrentIndex(box.findData("Клининг"))
    assert filter_page.current_filter().dds_item == "Клининг"

    box.setCurrentIndex(box.findData(dds.NO_ITEM))
    assert filter_page.current_filter().dds_item == dds.NO_ITEM


def test_статья_попадает_в_подпись_отбора(filter_page):
    box = filter_page.dds_filter
    box.setCurrentIndex(box.findData("Аренда"))

    filter_page._refresh_filters_summary()

    assert "статья ДДС: Аренда" in filter_page.filters_summary.text()


def test_сброс_возвращает_все_статьи(filter_page):
    box = filter_page.dds_filter
    box.setCurrentIndex(box.findData("Аренда"))

    filter_page.reset_filters()

    assert box.currentData() == "" and box.currentText() == "Все статьи ДДС"


def test_перезагрузка_списков_сохраняет_выбранную_статью(filter_page):
    box = filter_page.dds_filter
    box.setCurrentIndex(box.findData("Клининг"))

    filter_page._fill_filter_lists({"dds_items": ["Аренда", "Клининг", "Новая"]})

    assert box.currentData() == "Клининг"


def test_недописанный_текст_в_фильтре_откатывается(filter_page):
    box = filter_page.dds_filter
    box.setCurrentIndex(box.findData("Аренда"))
    box.lineEdit().setText("клин")

    box.lineEdit().editingFinished.emit()

    assert box.currentText() == "Аренда"


# --- напоминание про статью ДДС ------------------------------------------------

def test_закрыть_напоминание_можно_только_после_отсчёта(application):
    from app.ui.widgets.notice_dialog import NoticeDialog

    dialog = NoticeDialog("Заголовок", "Текст", delay=3)
    closed: list[int] = []
    dialog.rejected.connect(lambda: closed.append(1))

    assert not dialog.close_button.isEnabled()
    assert dialog.close_button.text() == "Закрыть (3)"
    dialog.reject()                      # Esc
    assert closed == []

    dialog._tick(); dialog._tick()
    assert not dialog.close_button.isEnabled()
    assert dialog.close_button.text() == "Закрыть (1)"

    dialog._tick()
    assert dialog.close_button.isEnabled() and dialog.close_button.text() == "Закрыть"
    assert not dialog._timer.isActive()
    dialog.reject()
    assert closed == [1]


def test_крестик_до_конца_отсчёта_не_закрывает(application):
    from PySide6.QtGui import QCloseEvent

    from app.ui.widgets.notice_dialog import NoticeDialog

    dialog = NoticeDialog("З", "Т", delay=5)
    event = QCloseEvent()

    dialog.closeEvent(event)

    assert not event.isAccepted()


def test_по_умолчанию_закрытие_через_десять_секунд(application):
    from app.ui.widgets.notice_dialog import NoticeDialog

    assert NoticeDialog("З", "Т").close_button.text() == "Закрыть (10)"


def test_флажок_больше_не_показывать(application):
    from app.ui.widgets.notice_dialog import NoticeDialog

    dialog = NoticeDialog("З", "Т", delay=0)
    assert dialog.close_button.isEnabled() and not dialog.hide_forever()

    dialog.dont_show.setChecked(True)
    assert dialog.hide_forever()


def test_отметка_не_показывать_сохраняется(tmp_path):
    path = str(tmp_path / "settings.json")
    settings = AppSettings.load(path)
    assert settings.payment_dds_notice_hidden is False

    settings.payment_dds_notice_hidden = True
    settings.save()

    assert AppSettings.load(path).payment_dds_notice_hidden is True


@pytest.fixture
def notice(filter_page, monkeypatch):
    """Страница оплат, у которой окно-напоминание подменено: что в нём и закрыто ли."""
    from app.ui import payments_page as module

    shown: list[str] = []

    class Fake:
        checked = False

        def __init__(self, title, text, **kwargs):
            shown.append(text)

        def exec(self):
            return 1

        def hide_forever(self):
            return Fake.checked

    monkeypatch.setattr(module, "NoticeDialog", Fake)
    monkeypatch.setattr(filter_page, "isVisible", lambda: True)
    filter_page.settings.save = lambda: None
    return filter_page, shown, Fake


def test_напоминание_про_маркетинг_показывается(notice):
    page, shown, _ = notice

    page._remind_dds_item()

    assert len(shown) == 1
    assert "маркетинг" in shown[0] and "Статью ДДС" in shown[0]
    assert page.settings.payment_dds_notice_hidden is False


def test_с_флажком_напоминание_больше_не_появляется(notice):
    page, shown, Fake = notice
    Fake.checked = True

    page._remind_dds_item()
    page._remind_dds_item()

    assert len(shown) == 1
    assert page.settings.payment_dds_notice_hidden is True


def test_без_флажка_напоминание_приходит_снова(notice):
    page, shown, _ = notice

    page._remind_dds_item()
    page._remind_dds_item()

    assert len(shown) == 2


def test_скрытая_страница_напоминание_не_показывает(notice):
    page, shown, _ = notice
    page.isVisible = lambda: False

    page._remind_dds_item()

    assert shown == []
