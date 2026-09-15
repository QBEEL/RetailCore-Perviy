"""Вкладка «Маркировка»: разбор кодов, вход и проверка.

Ни КриптоПро, ни «Честный ЗНАК» здесь не нужны: `service` подменяется целиком,
и проверяется то, что вкладка делает со своей стороны, — что уходит в запрос,
что показывает подпись и какие кнопки доступны.
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

from app.core.marking.codes import GS
from app.core.marking.crypto import Certificate
from app.core.marking.models import (
    CodeInfo,
    CodeState,
    Contour,
    Operation,
    OperationKind,
    OperationStatus,
)
from app.core.marking.orders import Buffer, Order, ReleaseMethod
from app.core.marking.suz import Credentials
from app.core.marking.session import Organisation, Session
from app.core.marking.transport import MarkingError
from app.core.settings import AppSettings

# Ответ боевого контура на вход без ИНН — дословно, вместе с его опечаткой.
NO_INN = ("Невозможно однозначно определить под какой организацией выполняется "
          "авторизация. Укажите запросе INN.")
IVAN = Organisation(inn="250101802801", name="ИП Саух Иван Васильевич")
MARIA = Organisation(inn="254009895745", name="ИП Саух Мария Николаевна")

# Настоящий по строению код парфюмерии — тот же, что в тестах разбора.
PERFUME = f"010460123456789321Abc123XyZ{GS}91EE10{GS}92signature=="
OTHER = f"010460123456789321Zzz999{GS}91EE11"


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def stub(monkeypatch):
    """Система маркировки, какой её видит вкладка, и журнал обращений к ней."""
    from app.ui import marking_page

    calls: dict[str, list] = {"checked": [], "recorded": [], "signed_in": [],
                              "organisations": [], "suz_checked": [],
                              "orders_asked": [], "ordered": []}
    state = {
        "signed": False,
        # Сертификат по машиночитаемой доверенности: без ИНН боевой контур
        # отказывается решать, под кем выполняется вход.
        "several": False,
        "organisations": [IVAN, MARIA],
        "directory_error": "",
        "session": Session(contour=Contour.SANDBOX, token="токен",
                           owner="Иванов Евгений", organisation="ООО «Ромашка»",
                           inn="2536000000"),
        "certificates": [Certificate(thumbprint="ААББ",
                                     subject="CN=Иванов Евгений, ИНН=2536000000",
                                     has_private_key=True)],
        "crypto": True,
        "answer": [],
        "history": [],
        # Реквизиты СУЗ, сохранённые «в профиле»: настоящий файл в тестах не
        # трогается — в нём лежит рабочий токен.
        "suz": {},
        "suz_answer": "Соединение с СУЗ установлено · ОМС 1234",
        "suz_error": "",
        "orders": [],
        "orders_error": "",
        # Реквизиты, которыми ядро в итоге воспользовалось. Не пустые означают
        # обновлённый по дороге токен.
        "suz_used": None,
        "order_id": "новый-заказ",
        "order_error": "",
    }

    def check(raw_codes, *, contour, use_cache, progress=None, db_path=None):
        calls["checked"].append(list(raw_codes))
        return list(state["answer"])

    def record_check(raw_codes, *, contour=None, comment="", db_path=None):
        calls["recorded"].append(list(raw_codes))
        return Operation(kind=OperationKind.CHECK, codes=list(raw_codes))

    def sign_in(thumbprint, inn="", contour=Contour.SANDBOX, organisation=""):
        calls["signed_in"].append((thumbprint, inn, contour))
        if state["several"] and not inn:
            raise MarkingError(NO_INN)
        state["signed"] = True
        return state["session"]

    def organisations(thumbprint, contour=Contour.SANDBOX):
        calls["organisations"].append((thumbprint, contour))
        if state["directory_error"]:
            raise MarkingError(state["directory_error"])
        return list(state["organisations"])

    def sign_out():
        state["signed"] = False

    def suz_credentials(contour=Contour.SANDBOX):
        return state["suz"].get(contour, Credentials())

    def save_suz(credentials, contour=Contour.SANDBOX):
        state["suz"][contour] = credentials.stripped()
        return state["suz"][contour]

    def forget_suz(contour=Contour.SANDBOX):
        state["suz"].pop(contour, None)

    def check_suz(credentials, contour=Contour.SANDBOX, thumbprint="", inn=""):
        calls["suz_checked"].append((credentials.stripped(), contour))
        if state["suz_error"]:
            raise MarkingError(state["suz_error"])
        # Пара «ответ и реквизиты, с которыми получилось»: токен мог обновиться
        # по дороге, и ядро возвращает тот, что теперь настоящий.
        return state["suz_answer"], state["suz_used"] or credentials.stripped()

    def suz_orders(credentials, contour=Contour.SANDBOX, thumbprint="", inn=""):
        calls["orders_asked"].append((credentials.stripped(), contour))
        if state["orders_error"]:
            raise MarkingError(state["orders_error"])
        return list(state["orders"]), state["suz_used"] or credentials.stripped()

    def create_suz_order(request, credentials, contour=Contour.SANDBOX,
                         thumbprint="", inn=""):
        calls["ordered"].append((request, contour))
        if state["order_error"]:
            raise MarkingError(state["order_error"])
        return state["order_id"], credentials.stripped()

    monkeypatch.setattr(marking_page.service, "create_suz_order", create_suz_order)
    monkeypatch.setattr(marking_page.service, "suz_orders", suz_orders)
    monkeypatch.setattr(marking_page.service, "suz_credentials", suz_credentials)
    monkeypatch.setattr(marking_page.service, "save_suz", save_suz)
    monkeypatch.setattr(marking_page.service, "forget_suz", forget_suz)
    monkeypatch.setattr(marking_page.service, "check_suz", check_suz)
    monkeypatch.setattr(marking_page.service, "restore_suz", lambda: None)
    monkeypatch.setattr(marking_page.service, "crypto_available",
                        lambda: state["crypto"])
    monkeypatch.setattr(marking_page.service, "certificates",
                        lambda: list(state["certificates"]))
    monkeypatch.setattr(marking_page.service, "signed_in", lambda: state["signed"])
    monkeypatch.setattr(marking_page.service, "current", lambda: state["session"])
    monkeypatch.setattr(marking_page.service, "sign_in", sign_in)
    monkeypatch.setattr(marking_page.service, "sign_out", sign_out)
    monkeypatch.setattr(marking_page.service, "organisations", organisations)
    monkeypatch.setattr(marking_page.service, "check", check)
    monkeypatch.setattr(marking_page.service, "record_check", record_check)
    monkeypatch.setattr(marking_page.service, "history",
                        lambda limit=50, db_path=None: list(state["history"]))
    return calls, state


@pytest.fixture
def dialog(monkeypatch):
    """Окно выбора организации, подменённое на запись о том, что ему показали."""
    from app.ui import marking_page

    box: dict = {"shown": [], "note": "", "current": "", "pick": MARIA,
                 "result": 1, "refresh": False, "added": []}

    class Fake:
        def __init__(self, organisations, current_inn="", parent=None, note=""):
            box["shown"] = list(organisations)
            box["current"] = current_inn
            box["note"] = note
            self.refresh_wanted = box["refresh"]
            self._known = list(organisations) + list(box["added"])

        def exec(self):
            return box["result"]

        @property
        def chosen(self):
            return box["pick"]

        @property
        def organisations(self):
            return self._known

    monkeypatch.setattr(marking_page, "OrganisationDialog", Fake)
    return box


def _page(application, tmp_path, *, restore: bool = True):
    """Собранная вкладка с дождавшимися своего часа фоновыми задачами."""
    from app.ui.marking_page import MarkingPage

    settings = AppSettings(_path=str(tmp_path / "settings.json"))
    page = MarkingPage(settings, lambda *_: None)
    if restore:
        page.restore()
        _settle(application)
    return page


def _settle(application) -> None:
    for _ in range(6):
        QThreadPool.globalInstance().waitForDone(3000)
        application.processEvents()


def _info(code: str, **kwargs) -> CodeInfo:
    item = CodeInfo(code=code, found=True, valid=True,
                    state=CodeState.INTRODUCED, our_inn="2536000000",
                    owner_inn="2536000000", product_name="Духи «Пример», 50 мл")
    for name, value in kwargs.items():
        setattr(item, name, value)
    return item


# --- разбор кодов ------------------------------------------------------------------

def test_коды_разбираются_без_входа_и_без_сети(application, tmp_path, stub):
    """Состав кода и повторы видны до всякого обращения к системе."""
    page = _page(application, tmp_path)
    page.codes_edit.setPlainText(f"{PERFUME}\n{OTHER}\n{PERFUME}")
    page._reparse()

    assert page.tile_codes._value.text() == "3"
    assert page.tile_items._value.text() == "1"      # товар один, экземпляра два
    assert page.tile_repeats._value.text() == "1"


def test_испорченный_код_назван_и_не_уходит_в_запрос(application, tmp_path, stub):
    calls, state = stub
    state["signed"] = True
    page = _page(application, tmp_path)
    page.codes_edit.setPlainText(f"{PERFUME}\n0104601234567893")
    page._reparse()

    assert page.tile_broken._value.text() == "1"
    assert "серийн" in page.codes_hint.text()

    page.run_check()
    _settle(application)

    assert calls["checked"] == [[PERFUME]]


# --- вход --------------------------------------------------------------------------

def test_без_входа_проверка_недоступна(application, tmp_path, stub):
    page = _page(application, tmp_path)
    page.codes_edit.setPlainText(PERFUME)
    page._reparse()

    assert not page.check_button.isEnabled()


def test_вход_запоминает_сертификат(application, tmp_path, stub):
    calls, _ = stub
    page = _page(application, tmp_path)

    page.sign_in()
    _settle(application)

    assert calls["signed_in"] == [("ААББ", "", Contour.SANDBOX)]
    assert page.settings.marking_thumbprint == "ААББ"
    assert "ООО «Ромашка»" in page.session_hint.text()


# --- выбор организации ---------------------------------------------------------------

def test_отказ_без_инн_открывает_выбор_а_не_ошибку(application, tmp_path, stub, dialog):
    """«Невозможно однозначно определить…» — это вопрос к пользователю."""
    calls, state = stub
    state["several"] = True
    page = _page(application, tmp_path)

    page.sign_in()
    _settle(application)

    assert [item.inn for item in dialog["shown"]] == [IVAN.inn, MARIA.inn]
    # После выбора вход повторяется — уже с ИНН.
    assert calls["signed_in"][-1] == ("ААББ", MARIA.inn, Contour.SANDBOX)
    assert state["signed"]


def test_выбранная_организация_запоминается(application, tmp_path, stub, dialog):
    _, state = stub
    state["several"] = True
    page = _page(application, tmp_path)

    page.sign_in()
    _settle(application)

    assert page.settings.marking_inn == MARIA.inn
    assert page.settings.marking_organisation == MARIA.name
    assert [item["inn"] for item in page.settings.marking_organisations] == [
        IVAN.inn, MARIA.inn]

    # До следующего входа видно, под кем он пойдёт: сертификат действует за
    # нескольких, и узнавать это после нажатия поздно.
    page.sign_out()
    assert MARIA.name in page.session_hint.text()


def test_запомненный_список_не_дёргает_ключ(application, tmp_path, stub, dialog):
    """Спрашивать систему заново — значит требовать пароль к контейнеру зря."""
    calls, _ = stub
    page = _page(application, tmp_path)
    page.settings.marking_organisations = [{"inn": IVAN.inn, "name": IVAN.name}]

    page.choose_organisation()
    _settle(application)

    assert calls["organisations"] == []
    assert [item.inn for item in dialog["shown"]] == [IVAN.inn]


def test_недоступный_справочник_не_запирает_вход(application, tmp_path, stub, dialog):
    """ИНН можно ввести руками: без него вход невозможен вовсе."""
    _, state = stub
    state["directory_error"] = "Сервис недоступен"
    page = _page(application, tmp_path)

    page.choose_organisation()
    _settle(application)

    assert dialog["shown"] == []
    assert "Сервис недоступен" in dialog["note"]


def test_отмена_выбора_не_входит(application, tmp_path, stub, dialog):
    calls, state = stub
    state["several"] = True
    dialog["result"] = 0
    page = _page(application, tmp_path)

    page.sign_in()
    _settle(application)

    assert not state["signed"]
    assert [inn for _, inn, _ in calls["signed_in"]] == [""]


def test_смена_сертификата_забывает_организации(application, tmp_path, stub, dialog):
    """Список выдан доверенностью на другой сертификат — переносить его нельзя."""
    _, state = stub
    state["certificates"] = [
        Certificate(thumbprint="ААББ", subject="CN=Первый, ИНН=1"),
        Certificate(thumbprint="ВВГГ", subject="CN=Второй, ИНН=2"),
    ]
    page = _page(application, tmp_path)
    page.settings.marking_inn = MARIA.inn
    page.settings.marking_organisations = [{"inn": MARIA.inn, "name": MARIA.name}]

    page.certificate_box.setCurrentIndex(1)

    assert page.settings.marking_thumbprint == "ВВГГ"
    assert page.settings.marking_inn == ""
    assert page.settings.marking_organisations == []


def test_смена_контура_сбрасывает_вход(application, tmp_path, stub):
    """Токен выдан для своего контура: с песочничным входом в бой ходить нельзя."""
    _, state = stub
    page = _page(application, tmp_path)
    page.sign_in()
    _settle(application)
    assert state["signed"]

    page.contour_box.setCurrentIndex(
        page.contour_box.findData(Contour.PRODUCTION.value))

    assert not state["signed"]
    assert page.settings.marking_contour == "production"


def test_боевой_контур_виден_до_нажатия(application, tmp_path, stub):
    page = _page(application, tmp_path)
    page.contour_box.setCurrentIndex(
        page.contour_box.findData(Contour.PRODUCTION.value))

    assert "Боевой контур" in page.session_hint.text()
    assert "color" in page.session_hint.styleSheet()


def test_без_криптопро_вкладка_объясняет_причину(application, tmp_path, stub):
    _, state = stub
    state["crypto"] = False
    state["certificates"] = []
    page = _page(application, tmp_path)

    assert "КриптоПро" in page.session_hint.text()
    assert not page.login_button.isEnabled()


# --- проверка ------------------------------------------------------------------------

def test_результат_раскладывается_по_колонкам(application, tmp_path, stub):
    calls, state = stub
    state["signed"] = True
    state["answer"] = [_info(PERFUME), _info(OTHER, state=CodeState.RETIRED)]
    page = _page(application, tmp_path)
    page.codes_edit.setPlainText(f"{PERFUME}\n{OTHER}")
    page._reparse()

    page.run_check()
    _settle(application)

    assert page.result.rowCount() == 2
    assert page.tile_circulation._value.text() == "1"
    assert page.tile_elsewhere._value.text() == "1"
    assert page.result.item(1, 2).text() == "Выведен из оборота"


def test_чужой_код_помечен(application, tmp_path, stub):
    """Чужой код в приёмке — повод остановиться, а не строка среди тысячи."""
    _, state = stub
    state["signed"] = True
    state["answer"] = [_info(PERFUME, owner_inn="7700000000",
                             owner_name="ООО «Чужое»")]
    page = _page(application, tmp_path)
    page.codes_edit.setPlainText(PERFUME)
    page._reparse()

    page.run_check()
    _settle(application)

    assert page.tile_alien._value.text() == "1"
    assert "чужой" in page.result.item(0, 3).text()


def test_только_замечания_отбирает_строки(application, tmp_path, stub):
    _, state = stub
    state["signed"] = True
    state["answer"] = [_info(PERFUME), _info(OTHER, found=False, valid=False,
                                             state=CodeState.UNKNOWN)]
    page = _page(application, tmp_path)
    page.codes_edit.setPlainText(f"{PERFUME}\n{OTHER}")
    page._reparse()
    page.run_check()
    _settle(application)

    page.problems_box.setChecked(True)

    assert page.result.rowCount() == 1
    assert page.tile_missing._value.text() == "1"


def test_проверка_попадает_в_журнал(application, tmp_path, stub):
    """По журналу видно, что и когда спрашивали, даже если окно закрыли сразу."""
    calls, state = stub
    state["signed"] = True
    state["answer"] = [_info(PERFUME)]
    page = _page(application, tmp_path)
    page.codes_edit.setPlainText(PERFUME)
    page._reparse()

    page.run_check()
    _settle(application)

    assert calls["recorded"] == [[PERFUME]]


# --- окно выбора организации ----------------------------------------------------------

def _dialog(organisations=(), current_inn=""):
    from app.ui.widgets.marking_dialogs import OrganisationDialog

    return OrganisationDialog(list(organisations), current_inn)


def test_окно_отмечает_организацию_прошлого_входа(application):
    window = _dialog([IVAN, MARIA], MARIA.inn)

    assert window.chosen == MARIA


def test_инн_вводится_руками(application):
    """Справочник ГИС МТ мог не ответить — вход не должен на этом кончиться."""
    window = _dialog()
    window.inn.setText(IVAN.inn)
    window.name.setText(IVAN.name)

    window.add_manually()

    assert [item.inn for item in window.organisations] == [IVAN.inn]
    assert window.chosen.name == IVAN.name


def test_негодный_инн_не_добавляется(application):
    window = _dialog()
    window.inn.setText("12345")

    window.add_manually()

    assert window.organisations == []
    assert window.chosen is None


def test_повтор_инн_не_задваивает_список(application):
    window = _dialog([IVAN])
    window.inn.setText(IVAN.inn)

    window.add_manually()

    assert len(window.organisations) == 1
    assert window.chosen == IVAN


# --- журнал --------------------------------------------------------------------------

def test_ждущая_операция_названа_ждущей(application, tmp_path, stub):
    """Опрос состояния выключен — молчать об этом нельзя."""
    _, state = stub
    state["history"] = [Operation(id=1, kind=OperationKind.WITHDRAWAL,
                                  codes=[PERFUME], status=OperationStatus.SENT)]
    page = _page(application, tmp_path)

    assert page.journal.rowCount() == 1
    assert page.journal.item(0, 1).text() == "Вывод из оборота"
    assert "Опрос состояния пока не подключён" in page.journal_hint.text()


# --- СУЗ -----------------------------------------------------------------------------

SUZ = Credentials(oms_id="oms-песочница", connection_id="соединение",
                  token="токен-устройства-длинный")


def _fill_suz(page, credentials: Credentials) -> None:
    page.oms_edit.setText(credentials.oms_id)
    page.connection_edit.setText(credentials.connection_id)
    page.token_edit.setText(credentials.token)


def test_реквизиты_суз_свои_у_каждого_контура(application, tmp_path, stub):
    """Устройство в песочнице и устройство в бою — разные.

    Оставить на экране чужие реквизиты значит однажды заказать коды не в той
    системе, поэтому смена контура перечитывает набор.
    """
    _, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ,
                    Contour.PRODUCTION: Credentials(oms_id="oms-боевой",
                                                    connection_id="другое",
                                                    token="другой-токен")}
    page = _page(application, tmp_path)

    assert page.oms_edit.text() == "oms-песочница"

    page.contour_box.setCurrentIndex(
        page.contour_box.findData(Contour.PRODUCTION.value))

    assert page.oms_edit.text() == "oms-боевой"
    assert page.token_edit.text() == "другой-токен"


def test_удачная_проверка_сохраняет_реквизиты(application, tmp_path, stub):
    """Иначе настроенное соединение не переживёт перезапуск."""
    calls, state = stub
    page = _page(application, tmp_path)
    _fill_suz(page, SUZ)

    page.check_suz()
    _settle(application)

    assert calls["suz_checked"] == [(SUZ, Contour.SANDBOX)]
    assert state["suz"][Contour.SANDBOX] == SUZ
    assert state["suz_answer"] in page.suz_hint.text()


def test_отказ_суз_ничего_не_сохраняет(application, tmp_path, stub):
    calls, state = stub
    state["suz_error"] = "СУЗ не приняла токен"
    page = _page(application, tmp_path)
    _fill_suz(page, SUZ)

    page.check_suz()
    _settle(application)

    assert state["suz"] == {}
    assert "СУЗ не приняла токен" in page.suz_hint.text()


def test_токен_суз_не_видно_на_экране(application, tmp_path, stub):
    """Реквизиты вписывают при коллегах и на общем экране."""
    from PySide6.QtWidgets import QLineEdit

    page = _page(application, tmp_path)

    assert page.token_edit.echoMode() == QLineEdit.EchoMode.Password

    page.token_shown.setChecked(True)

    assert page.token_edit.echoMode() == QLineEdit.EchoMode.Normal


def test_нехватка_реквизита_названа_до_обращения_к_суз(application, tmp_path, stub):
    """Идентификатор соединения в нехватку не входит: он никуда не уходит."""
    page = _page(application, tmp_path)
    page.oms_edit.setText(SUZ.oms_id)

    assert "токен" in page.suz_hint.text()
    assert "идентификатор соединения" not in page.suz_hint.text()

    page.token_edit.setText(SUZ.token)

    assert "Не хватает" not in page.suz_hint.text()


def test_проверка_суз_не_требует_входа_по_сертификату(application, tmp_path, stub):
    """Это другая система и другой способ представиться."""
    _, state = stub
    state["signed"] = False
    page = _page(application, tmp_path)

    assert not page.check_button.isEnabled()
    assert page.suz_check_button.isEnabled()


def test_забытые_реквизиты_исчезают_и_с_экрана(application, tmp_path, stub):
    _, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    page = _page(application, tmp_path)

    page.forget_suz()

    assert state["suz"] == {}
    assert page.oms_edit.text() == ""
    assert page.token_edit.text() == ""


def test_токен_суз_добывается_сертификатом_и_сразу_проверяется(application, tmp_path,
                                                               stub, monkeypatch):
    """Ответ на «токен из другой программы не подошёл»: берём свой.

    Проверка следом — не лишний запрос: токен мог быть выдан участнику, а не
    той станции, чей ОМС ID вписан.
    """
    from app.ui import marking_page

    calls, state = stub
    calls["suz_signed_in"] = []

    def suz_sign_in(credentials, thumbprint, inn="", contour=Contour.SANDBOX):
        calls["suz_signed_in"].append((thumbprint, contour))
        return Credentials(oms_id=credentials.oms_id, token="токен-от-суз",
                           issued_at="2026-08-05T14:30")

    monkeypatch.setattr(marking_page.service, "suz_sign_in", suz_sign_in)
    page = _page(application, tmp_path)
    page.oms_edit.setText(SUZ.oms_id)

    page.sign_in_suz()
    _settle(application)

    assert calls["suz_signed_in"] == [("ААББ", Contour.SANDBOX)]
    assert page.token_edit.text() == "токен-от-суз"
    assert state["suz"][Contour.SANDBOX].token == "токен-от-суз"
    # Полученный токен тут же проверен на этой станции.
    assert calls["suz_checked"] == [(state["suz"][Contour.SANDBOX], Contour.SANDBOX)]


def test_без_сертификата_токен_суз_просить_не_у_кого(application, tmp_path, stub):
    _, state = stub
    state["certificates"] = []
    page = _page(application, tmp_path)

    assert not page.suz_token_button.isEnabled()


# --- заказы кодов ---------------------------------------------------------------------

def _order(**kwargs) -> Order:
    order = Order(id="3a8f-0001", status="READY", created_at=None,
                  buffers=[Buffer(gtin="04601234567893", left=250, available=250,
                                  total=1000, passed=750, status="ACTIVE")])
    for name, value in kwargs.items():
        setattr(order, name, value)
    return order


def test_заказы_показывают_сколько_кодов_осталось(application, tmp_path, stub):
    """Ради этого числа заказы и смотрят: «сколько заказывали» не отвечает на «хватит ли»."""
    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    state["orders"] = [_order()]
    page = _page(application, tmp_path)
    _settle(application)

    assert calls["orders_asked"] == [(SUZ, Contour.SANDBOX)]
    assert page.orders.item(0, 0).text() == "3a8f-0001"
    assert page.orders.item(0, 2).text() == "Готов"
    assert page.orders.item(0, 4).text() == "750"      # получено
    assert page.orders.item(0, 5).text() == "250"      # доступно к получению
    assert "кодов доступно к получению: 250" in page.orders_hint.text()


def test_незнакомое_состояние_заказа_не_выдаётся_за_готовое(application, tmp_path, stub):
    _, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    state["orders"] = [_order(status="WAITING_FOR_SIGN")]
    page = _page(application, tmp_path)
    _settle(application)

    assert page.orders.item(0, 2).text() == "WAITING_FOR_SIGN"


def test_без_реквизитов_заказы_не_спрашиваются(application, tmp_path, stub):
    calls, _ = stub
    page = _page(application, tmp_path)
    _settle(application)

    assert calls["orders_asked"] == []
    assert page.orders.rowCount() == 0
    assert "настройте соединение" in page.orders_hint.text()


def test_смена_контура_убирает_чужие_заказы(application, tmp_path, stub):
    """За заказы отвечала другая станция — оставить их на экране нельзя."""
    _, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    state["orders"] = [_order()]
    page = _page(application, tmp_path)
    _settle(application)
    assert page.orders.rowCount() == 1

    page.contour_box.setCurrentIndex(
        page.contour_box.findData(Contour.PRODUCTION.value))

    assert page.orders.rowCount() == 0


def test_отказ_на_заказах_не_молчит(application, tmp_path, stub):
    _, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    state["orders_error"] = "состав ответа незнаком"
    page = _page(application, tmp_path)
    _settle(application)

    assert "состав ответа незнаком" in page.orders_hint.text()


# --- новый заказ ----------------------------------------------------------------------

@pytest.fixture
def confirm(monkeypatch):
    """Окно подтверждения заказа, подменённое на запись о том, что ему показали."""
    from app.ui import marking_page

    box: dict = {"shown": None, "contour": None, "result": 1}

    class Fake:
        def __init__(self, request, contour, parent=None):
            box["shown"] = request
            box["contour"] = contour

        def exec(self):
            return box["result"]

    monkeypatch.setattr(marking_page, "OrderConfirmDialog", Fake)
    return box


def _order_form(page, gtin="04601234567893", quantity="500") -> None:
    page.contact_edit.setText("Иванов Евгений")
    page.add_line()
    page.lines.item(0, 0).setText(gtin)
    page.lines.item(0, 1).setText(quantity)


def test_заказ_спрашивает_подтверждение_и_показывает_что_уйдёт(application, tmp_path,
                                                               stub, confirm):
    """Привычка нажимать не глядя вырабатывается в песочнице, а срабатывает в бою."""
    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    page = _page(application, tmp_path)
    _order_form(page)

    page.create_order()
    _settle(application)

    assert confirm["shown"].total == 500
    assert confirm["shown"].lines[0].gtin == "04601234567893"
    assert len(calls["ordered"]) == 1


def test_отказ_в_подтверждении_ничего_не_отправляет(application, tmp_path, stub,
                                                    confirm):
    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    confirm["result"] = 0
    page = _page(application, tmp_path)
    _order_form(page)

    page.create_order()
    _settle(application)

    assert calls["ordered"] == []


def test_негодный_заказ_не_доходит_до_подтверждения(application, tmp_path, stub,
                                                    confirm):
    """Проверка до денег: одинаковые товары СУЗ не примет."""
    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    page = _page(application, tmp_path)
    _order_form(page)
    page.add_line()
    page.lines.item(1, 0).setText("04601234567893")
    page.lines.item(1, 1).setText("10")

    page.create_order()
    _settle(application)

    assert confirm["shown"] is None
    assert calls["ordered"] == []
    assert "одинаковые коды товаров" in page.order_hint.text().lower()


def test_созданный_заказ_очищает_строки_и_обновляет_список(application, tmp_path,
                                                           stub, confirm):
    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    page = _page(application, tmp_path)
    _order_form(page)
    asked = len(calls["orders_asked"])

    page.create_order()
    _settle(application)

    assert page.lines.rowCount() == 0
    assert "новый-заказ" in page.order_hint.text()
    assert len(calls["orders_asked"]) > asked


def test_неизвестный_исход_заказа_ведёт_к_списку_а_не_к_повтору(application, tmp_path,
                                                                stub, confirm):
    """Повтор создания — это второй заказ и вторые деньги."""
    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    state["order_error"] = ("Связь оборвалась, и что стало с заказом — неизвестно. "
                            "Обновите список заказов")
    page = _page(application, tmp_path)
    _order_form(page)
    asked = len(calls["orders_asked"])

    page.create_order()
    _settle(application)

    assert "неизвестно" in page.order_hint.text()
    # Список перечитан сам: заказ мог быть создан, и увидеть это нужно до того,
    # как захочется нажать ещё раз.
    assert len(calls["orders_asked"]) > asked


def test_производство_названо_неготовым_на_экране(application, tmp_path, stub):
    _, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    page = _page(application, tmp_path)
    _order_form(page)
    page.method_box.setCurrentIndex(
        page.method_box.findData(ReleaseMethod.PRODUCTION.value))

    assert "производственной площадке" in page.order_hint.text()


def test_сводка_заказа_согласована_по_числам(application, tmp_path, stub):
    """«1 товаров» отвлекает ровно там, где отвлекаться нельзя."""
    from app.ui.marking_page import _plural

    assert _plural(1, "товар", "товара", "товаров") == "1 товар"
    assert _plural(3, "товар", "товара", "товаров") == "3 товара"
    assert _plural(5, "код", "кода", "кодов") == "5 кодов"
    assert _plural(11, "код", "кода", "кодов") == "11 кодов"
    assert _plural(21, "код", "кода", "кодов") == "21 код"

    _, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    page = _page(application, tmp_path)
    _order_form(page, quantity="500")

    assert "1 товар," in page.order_hint.text()
    assert "500 кодов" in page.order_hint.text()


def test_шаблон_подставляется_из_прошлого_заказа_и_объясняется(application, tmp_path,
                                                               stub):
    """Число «10» само по себе не значит ничего — важно, откуда оно взялось."""
    from datetime import datetime

    _, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    past = Order(id="з-1", status="READY", product_group="lp",
                 created_at=datetime(2026, 8, 3), raw={"paymentType": 2})
    past.buffers = [Buffer(gtin="046", raw={"templateId": 14})]
    state["orders"] = [past]
    page = _page(application, tmp_path)
    _settle(application)

    page.group_box.setCurrentIndex(page.group_box.findData("lp"))

    # Справочник для «lp» знает шаблон 10, но СУЗ однажды приняла 14 —
    # реальность старше документа, и выбран именно он.
    assert page.template_box.currentData() == 14
    assert page.payment_box.value() == 2
    assert "03.08.2026" in page.template_hint.text()
    assert page.order_request.problems == () or "шаблон" not in         " ".join(page.order_request.problems)


def test_группа_без_истории_берёт_шаблон_из_справочника(application, tmp_path, stub):
    """Именно на этом и споткнулся первый заказ: духам подставлялся шаблон «lp»."""
    from datetime import datetime

    _, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    past = Order(id="з-1", status="READY", product_group="lp",
                 created_at=datetime(2026, 8, 3), raw={"paymentType": 2})
    past.buffers = [Buffer(gtin="046", raw={"templateId": 10})]
    state["orders"] = [past]
    page = _page(application, tmp_path)
    _settle(application)

    page.group_box.setCurrentIndex(page.group_box.findData("perfumery"))

    assert page.template_box.currentData() == 9
    assert "справочника СУЗ" in page.template_hint.text()
    # И номер не единственное, что видно: шаблоны различаются длиной серийного
    # номера и криптохвостом, это и написано в строке.
    assert "серийный номер 13 знаков" in page.template_box.currentText()


def test_заказ_без_сертификата_не_доходит_до_подтверждения(application, tmp_path,
                                                           stub, confirm):
    """Заказ подписывается — сказать об этом нужно до подтверждения."""
    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    state["certificates"] = []
    page = _page(application, tmp_path)
    _order_form(page)

    page.create_order()
    _settle(application)

    assert confirm["shown"] is None
    assert calls["ordered"] == []
    assert "не выбран сертификат" in page.order_hint.text()
