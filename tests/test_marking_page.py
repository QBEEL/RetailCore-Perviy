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
from PySide6.QtWidgets import QApplication, QLabel

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
# Тот же код, каким он возвращается из проверки: без криптохвоста.
KI = "010460123456789321Abc123XyZ"


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


def test_результат_показывает_все_коды_без_обрезки(application, tmp_path, stub):
    """В документе бывает больше тысячи кодов, и глазами просят увидеть каждый."""
    page = _page(application, tmp_path)

    page._show_result([_info(f"code-{number}") for number in range(1200)])

    assert page.result.rowCount() == 1200
    assert "показаны первые" not in page.result_hint.text()


def test_двойной_щелчок_открывает_сведения_о_коде(application, tmp_path, stub, monkeypatch):
    """С отбором «только замечания» номер строки не равен номеру кода."""
    from app.ui import marking_page

    opened: list[CodeInfo] = []

    class Spy:
        action = ""
        PRINT, REMARK, WITHDRAW = (marking_page.CodeInfoDialog.PRINT,
                                   marking_page.CodeInfoDialog.REMARK,
                                   marking_page.CodeInfoDialog.WITHDRAW)

        def __init__(self, info, parent=None, **_):
            opened.append(info)

        def exec(self):
            return 0

    monkeypatch.setattr(marking_page, "CodeInfoDialog", Spy)
    page = _page(application, tmp_path)
    broken = _info(OTHER, found=False, valid=False, state=CodeState.UNKNOWN)
    page._show_result([_info(PERFUME), broken])
    page.problems_box.setChecked(True)

    page._on_result_activated(0, 1)

    assert opened == [broken]


def test_таблицу_результата_можно_развернуть(application, tmp_path, stub):
    page = _page(application, tmp_path)

    page.result_expand.click()
    assert page.result.minimumHeight() > 0
    assert page.result_expand.text() == "Свернуть"

    page.result_expand.click()
    assert page.result.minimumHeight() == 0


def _choose_in_dialog(monkeypatch, action: str) -> None:
    """Подменяет окно кода: пользователь сразу нажимает нужную кнопку."""
    from app.ui import marking_page

    class Chosen(marking_page.CodeInfoDialog):
        def exec(self):
            self.action = action
            return 1

    monkeypatch.setattr(marking_page, "CodeInfoDialog", Chosen)


def test_вывод_из_оборота_переносит_код_на_вкладку_вывода(
        application, tmp_path, stub, monkeypatch):
    from app.ui import marking_page

    _choose_in_dialog(monkeypatch, "withdraw")
    page = _page(application, tmp_path)
    page._show_result([_info(PERFUME)])

    page._on_result_activated(0, 0)

    assert page.withdrawal_tab.codes == [PERFUME]
    assert page.tabs.currentIndex() == marking_page.WITHDRAWAL_TAB


def test_печать_кода_идёт_блоком_в_памяти(application, tmp_path, stub, monkeypatch):
    from app.core.marking import issue
    from app.ui import marking_page

    shown: list = []

    class Spy:
        def __init__(self, batch, parent=None):
            shown.append(batch)

        def exec(self):
            return 0

    saved: list = []
    monkeypatch.setattr(issue, "save", lambda batch: saved.append(batch))
    monkeypatch.setattr(marking_page, "PrintDialog", Spy)
    _choose_in_dialog(monkeypatch, "print")
    page = _page(application, tmp_path)
    # Сканер отдал код без разделителей, а проверка вернула его без хвоста.
    page.codes_edit.setPlainText(PERFUME.replace(GS, ""))
    page._reparse()
    page._show_result([_info(KI)])

    page._on_result_activated(0, 0)
    issue.mark_printed(shown[0], 1)

    assert shown[0].codes == [PERFUME] and shown[0].id == ""
    assert saved == []


def test_код_без_криптохвоста_не_печатается(application, tmp_path, stub, monkeypatch):
    """Этикетка без ключа проверки — «сомнительный товар» в приложении ЧЗ."""
    from app.ui import marking_page

    seen: list = []

    class Spy(marking_page.CodeInfoDialog):
        def exec(self):
            seen.append(self.print_button.isEnabled())
            self.action = "print"
            return 1

    printed: list = []
    monkeypatch.setattr(marking_page, "CodeInfoDialog", Spy)
    monkeypatch.setattr(marking_page, "PrintDialog",
                        lambda *args, **kwargs: printed.append(args))
    page = _page(application, tmp_path)
    page._batches = []
    page.codes_edit.setPlainText(KI)
    page._reparse()
    page._show_result([_info(KI)])

    page._on_result_activated(0, 0)

    assert seen == [False] and printed == []


def test_криптохвост_берётся_из_блока_суз(application, tmp_path, stub):
    from app.core.marking import issue

    page = _page(application, tmp_path)
    page._batches = [issue.Batch(id="b1", gtin="04601234567893", codes=[PERFUME])]

    assert page._full_code(_info(KI)) == PERFUME


def test_перемаркировка_без_блока_с_кодами_объясняет_что_делать(
        application, tmp_path, stub, monkeypatch):
    _choose_in_dialog(monkeypatch, "remark")
    page = _page(application, tmp_path)
    page._batches = []
    toasts: list = []
    page.notify = lambda text, kind: toasts.append(text)
    page._show_result([_info(PERFUME, gtin="04650139031961")])

    page._on_result_activated(0, 0)

    assert toasts and "Заказ кодов" in toasts[0]


def test_перемаркировка_печатает_новый_код_и_ведёт_старый_в_предыдущие(
        application, tmp_path, stub, monkeypatch):
    from app.core.marking import issue
    from app.ui import marking_page

    batch = issue.Batch(id="b1", gtin="04650139031961", name="Духи",
                        codes=["new-1", "new-2"], printed=1)
    introduced: list = []

    class PrintSpy:
        def __init__(self, target, parent=None):
            self.target = target
            self.count = type("Spin", (), {"setValue": lambda s, v: None})()

        def exec(self):
            self.target.printed += 1
            return 0

    class IntroduceSpy:
        def __init__(self, target, inn, contour, problem, parent=None, *,
                     previous=(), fixed_range=None):
            introduced.append((target, tuple(previous), fixed_range))

        def exec(self):
            return 0

    monkeypatch.setattr(marking_page, "PrintDialog", PrintSpy)
    monkeypatch.setattr(marking_page, "IntroduceDialog", IntroduceSpy)
    monkeypatch.setattr(marking_page.MarkingPage, "reload_batches",
                        lambda self, select="": None)
    _choose_in_dialog(monkeypatch, "remark")
    page = _page(application, tmp_path)
    page._batches = [batch]
    page._show_result([_info(PERFUME, gtin="04650139031961")])

    page._on_result_activated(0, 0)

    assert introduced == [(batch, (PERFUME,), (1, 2))]


def test_перемаркировка_без_печати_ничего_не_вводит(
        application, tmp_path, stub, monkeypatch):
    from app.core.marking import issue
    from app.ui import marking_page

    batch = issue.Batch(id="b1", gtin="04650139031961", codes=["new-1"])
    introduced: list = []

    class PrintSpy:
        def __init__(self, target, parent=None):
            self.count = type("Spin", (), {"setValue": lambda s, v: None})()

        def exec(self):
            return 0

    monkeypatch.setattr(marking_page, "PrintDialog", PrintSpy)
    monkeypatch.setattr(marking_page.MarkingPage, "_introduce",
                        lambda self, *args, **kwargs: introduced.append(args))
    monkeypatch.setattr(marking_page.MarkingPage, "reload_batches",
                        lambda self, select="": None)
    _choose_in_dialog(monkeypatch, "remark")
    page = _page(application, tmp_path)
    page._batches = [batch]
    page._show_result([_info(PERFUME, gtin="04650139031961")])

    page._on_result_activated(0, 0)

    assert introduced == []


def test_окно_кода_кнопки_зависят_от_состояния(application):
    from app.ui.widgets.marking_dialogs import CodeInfoDialog

    live = CodeInfoDialog(_info(PERFUME, gtin="04650139031961"))
    retired = CodeInfoDialog(_info(PERFUME, gtin="04650139031961",
                                   state=CodeState.RETIRED))

    assert live.withdraw_button.isEnabled()
    assert not retired.withdraw_button.isEnabled()
    assert retired.withdraw_button.toolTip()
    assert retired.print_button.isEnabled() and retired.remark_button.isEnabled()

    live.remark_button.click()
    assert live.action == CodeInfoDialog.REMARK


def test_позиции_документа_можно_развернуть(application, tmp_path, stub):
    page = _page(application, tmp_path)

    page.progress_expand.click()
    assert page.progress.minimumHeight() > 0
    assert page.progress_expand.text() == "Свернуть"
    assert page.result.minimumHeight() == 0

    page.progress_expand.click()
    assert page.progress.minimumHeight() == 0


def test_окно_кода_показывает_владельца_и_ответ(application):
    from app.ui.widgets.marking_dialogs import CodeInfoDialog

    item = _info(PERFUME, raw={"productName": "Духи"}, owner_name="ИП Пример")

    dialog = CodeInfoDialog(item)

    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert any("ИП Пример" in text for text in texts)
    assert any(PERFUME == text for text in texts)


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


def _request(gtin="04601234567893", quantity=500, *extra):
    """Заказ, каким его собирает окно заказа."""
    from app.core.marking.orders import Line, Request

    lines = [Line(gtin=gtin, quantity=quantity, template_id=9)]
    lines += [Line(gtin=g, quantity=q, template_id=9) for g, q in extra]
    return Request(product_group="perfumery", method=ReleaseMethod.REMAINS,
                   contact="Иванов Евгений", lines=lines)


def test_заказ_спрашивает_подтверждение_и_показывает_что_уйдёт(application, tmp_path,
                                                               stub, confirm):
    """Привычка нажимать не глядя вырабатывается в песочнице, а срабатывает в бою."""
    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    page = _page(application, tmp_path)

    page.create_order(_request())
    _settle(application)

    assert confirm["shown"].total == 500
    assert confirm["shown"].lines[0].gtin == "04601234567893"
    assert len(calls["ordered"]) == 1


def test_несколько_товаров_уходят_одним_заказом(application, tmp_path, stub, confirm):
    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    page = _page(application, tmp_path)

    page.create_order(_request("04601234567893", 100, ("04607177964080", 250),
                               ("04600000000007", 30)))
    _settle(application)

    assert len(calls["ordered"]) == 1
    sent = calls["ordered"][0][0]
    assert [line.gtin for line in sent.lines] == [
        "04601234567893", "04607177964080", "04600000000007"]
    assert sent.total == 380


def test_отказ_в_подтверждении_ничего_не_отправляет(application, tmp_path, stub,
                                                    confirm):
    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    confirm["result"] = 0
    page = _page(application, tmp_path)

    page.create_order(_request())
    _settle(application)

    assert calls["ordered"] == []


def test_негодный_заказ_не_доходит_до_подтверждения(application, tmp_path, stub,
                                                    confirm):
    """Проверка до денег: одинаковые товары СУЗ не примет."""
    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    page = _page(application, tmp_path)

    page.create_order(_request("04601234567893", 500, ("04601234567893", 10)))
    _settle(application)

    assert confirm["shown"] is None
    assert calls["ordered"] == []


def test_созданный_заказ_сообщает_номер_и_обновляет_список(application, tmp_path,
                                                           stub, confirm):
    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    page = _page(application, tmp_path)
    asked = len(calls["orders_asked"])

    page.create_order(_request())
    _settle(application)

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
    asked = len(calls["orders_asked"])

    page.create_order(_request())
    _settle(application)

    assert "неизвестно" in page.order_hint.text()
    # Список перечитан сам: заказ мог быть создан, и увидеть это нужно до того,
    # как захочется нажать ещё раз.
    assert len(calls["orders_asked"]) > asked


def test_заказ_без_сертификата_не_доходит_до_подтверждения(application, tmp_path,
                                                           stub, confirm):
    """Заказ подписывается — сказать об этом нужно до подтверждения."""
    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    state["certificates"] = []
    page = _page(application, tmp_path)

    page.create_order(_request())
    _settle(application)

    assert confirm["shown"] is None
    assert calls["ordered"] == []


def test_окно_заказа_открывается_из_страницы_и_отправляет_собранное(
        application, tmp_path, stub, confirm, monkeypatch):
    from app.ui import marking_page

    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    seen: dict = {}

    class FakeOrder:
        def __init__(self, orders, has_certificate, contact="", parent=None):
            seen["orders"] = list(orders)
            seen["certificate"] = has_certificate
            seen["contact"] = contact

        def exec(self):
            return 1

        request = _request("04601234567893", 40, ("04607177964080", 60))

    monkeypatch.setattr(marking_page, "OrderDialog", FakeOrder)
    page = _page(application, tmp_path)
    _settle(application)

    page.new_order()
    _settle(application)

    # Окну отдаётся то, что нужно для подсказок: заказы, сертификат и имя.
    assert seen["certificate"] is True
    assert seen["contact"] == "Иванов Евгений"
    assert calls["ordered"][0][0].total == 100


def test_без_реквизитов_суз_окно_заказа_не_открывается(application, tmp_path, stub,
                                                       monkeypatch):
    from app.ui import marking_page

    opened: list = []
    monkeypatch.setattr(marking_page, "OrderDialog",
                        lambda *args, **kwargs: opened.append(1))
    page = _page(application, tmp_path)

    page.new_order()

    assert opened == []


def test_сводка_заказа_согласована_по_числам():
    """«1 товаров» отвлекает ровно там, где отвлекаться нельзя."""
    from app.ui.marking_page import _plural

    assert _plural(1, "товар", "товара", "товаров") == "1 товар"
    assert _plural(3, "товар", "товара", "товаров") == "3 товара"
    assert _plural(5, "код", "кода", "кодов") == "5 кодов"
    assert _plural(11, "код", "кода", "кодов") == "11 кодов"
    assert _plural(21, "код", "кода", "кодов") == "21 код"


# --- получение кодов, печать, ввод в оборот --------------------------------------------

def _issue_page(application, tmp_path, stub, monkeypatch):
    """Вкладка с заказом в списке и подменёнными окнами получения и печати."""
    from app.core.marking import issue
    from app.ui import marking_page

    calls, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    state["orders"] = [_order()]
    seen: dict = {"fetched": [], "printed": [], "asked": []}

    def product_info(credentials, order_id, contour=Contour.SANDBOX,
                     thumbprint="", inn=""):
        seen["asked"].append(order_id)
        return {"04601234567893": issue.Product(gtin="04601234567893",
                                                name="Духи", tnved="3303001000")}, \
            credentials.stripped()

    def fetch(credentials, order_id, gtin, quantity, contour=Contour.SANDBOX,
              thumbprint="", inn="", *, product=None, product_group="",
              release_method=""):
        seen["fetched"].append((order_id, gtin, quantity, product))
        batch = issue.Batch(id="blk-1", gtin=gtin, name=product.name,
                            codes=[PERFUME] * quantity, created="2026-10-05T10:00:00")
        issue.save(batch)
        return batch, credentials.stripped()

    class FakeIssue:
        def __init__(self, order, products, contour, parent=None):
            seen["dialog_products"] = dict(products)

        def exec(self):
            return 1

        gtin = "04601234567893"
        count = 3
        product = issue.Product(gtin="04601234567893", name="Духи", tnved="3303001000")

    class FakePrint:
        def __init__(self, batch, parent=None):
            seen["printed"].append(batch.id)

        def exec(self):
            return 0

    monkeypatch.setattr(marking_page.service, "suz_product_info", product_info)
    monkeypatch.setattr(marking_page.service, "fetch_suz_codes", fetch)
    monkeypatch.setattr(marking_page, "IssueDialog", FakeIssue)
    monkeypatch.setattr(marking_page, "PrintDialog", FakePrint)
    page = _page(application, tmp_path)
    _settle(application)
    page.orders.selectRow(0)
    return page, seen


def test_коды_забираются_сохраняются_и_сразу_идут_на_печать(application, tmp_path,
                                                            stub, monkeypatch):
    page, seen = _issue_page(application, tmp_path, stub, monkeypatch)

    page.fetch_codes()
    _settle(application)

    assert seen["asked"] == ["3a8f-0001"]
    assert seen["dialog_products"]["04601234567893"].tnved == "3303001000"
    assert seen["fetched"][0][:3] == ("3a8f-0001", "04601234567893", 3)
    # Блок лежит на диске и виден в списке, а окно печати открылось само.
    assert page.batches.rowCount() == 1
    assert page.batches.item(0, 2).text() == "3"
    assert seen["printed"] == ["blk-1"]
    assert page.order_tabs.currentIndex() == 1


def test_без_выбранного_заказа_коды_не_забираются(application, tmp_path, stub,
                                                  monkeypatch):
    page, seen = _issue_page(application, tmp_path, stub, monkeypatch)
    page.orders.clearSelection()
    page.orders.setCurrentCell(-1, -1)

    page.fetch_codes()
    _settle(application)

    assert seen["asked"] == [] and seen["fetched"] == []


def test_заказ_без_остатка_в_буфере_не_предлагает_получение(application, tmp_path,
                                                            stub, monkeypatch):
    _, state = stub
    state["orders"] = [_order(buffers=[Buffer(gtin="04601234567893", left=0, passed=10,
                                              total=10, status="EXHAUSTED")])]
    page, seen = _issue_page(application, tmp_path, stub, monkeypatch)
    state["orders"] = [_order(buffers=[Buffer(gtin="04601234567893", left=0, passed=10,
                                              total=10, status="EXHAUSTED")])]
    page.reload_orders()
    _settle(application)
    page.orders.selectRow(0)

    page.fetch_codes()
    _settle(application)

    assert seen["asked"] == []


def test_сохранённые_блоки_видны_после_перезапуска(application, tmp_path, stub,
                                                   monkeypatch):
    from app.core.marking import issue

    issue.save(issue.Batch(id="old", gtin="04601234567893", name="Духи",
                           codes=[PERFUME] * 5, printed=2,
                           created="2026-10-01T09:00:00"))
    page = _page(application, tmp_path)

    assert page.batches.rowCount() == 1
    assert page.batches.item(0, 3).text() == "2 из 5"


# --- ввод в оборот из программы --------------------------------------------------------

def _intro_page(application, tmp_path, stub, monkeypatch, *, action="send", group="lp",
                signed=True, contour=Contour.SANDBOX):
    """Вкладка с одним блоком кодов и подменёнными окном ввода и отправкой."""
    from app.core.marking import introduce, issue
    from app.ui import marking_page

    _, state = stub
    state["signed"] = signed
    state["session"].contour = contour
    batch = issue.Batch(id="blk-9", gtin="04601234567893", name="Колготки",
                        tnved="6115950000", product_group=group,
                        contour=Contour.SANDBOX.value, codes=[PERFUME] * 5, printed=5,
                        created="2026-10-05T10:00:00")
    issue.save(batch)
    seen: dict = {"dialogs": [], "sent": []}

    class FakeIntro:
        def __init__(self, batch, inn, contour, send_problem="", parent=None, **_):
            seen["dialogs"].append({"inn": inn, "problem": send_problem})
            self.saved_path = "C:/x.xml"
            self.covered = 5
            self.action = action
            self.document = introduce.Document(
                "250101802801", "6115950000", tuple(batch.codes))

        def exec(self):
            return 1

    def send(document, thumbprint, product_group, contour, *, on_created=None, **_):
        seen["sent"].append((document, thumbprint, product_group, contour))
        if on_created:
            on_created("doc-1")
        return seen["result"]

    seen["result"] = introduce.Sent("doc-1", introduce.DocStatus("CHECKED_OK"))
    monkeypatch.setattr(marking_page, "IntroduceDialog", FakeIntro)
    monkeypatch.setattr(marking_page.introduce_module, "send", send)
    page = _page(application, tmp_path)
    page.batches.selectRow(0)
    return page, seen


def test_отправленный_документ_запоминается_и_показывает_итог(application, tmp_path,
                                                             stub, monkeypatch):
    from app.core.marking import issue

    page, seen = _intro_page(application, tmp_path, stub, monkeypatch)

    page.introduce_batch()
    _settle(application)

    document, thumbprint, group, contour = seen["sent"][0]
    assert len(document.codes) == 5 and thumbprint == "ААББ" and group == "lp"
    saved = issue.load("blk-9")
    assert saved.doc_id == "doc-1" and saved.introduced_count == 5
    assert saved.doc_status == "CHECKED_OK"
    assert "Проверен" in page.batches.item(0, 4).text()
    assert "5 из 5" in page.batches.item(0, 4).text()


def test_номер_документа_сохранён_даже_если_итога_нет(application, tmp_path, stub,
                                                      monkeypatch):
    from app.core.marking import introduce, issue

    page, seen = _intro_page(application, tmp_path, stub, monkeypatch)
    seen["result"] = introduce.Sent("doc-1", introduce.DocStatus("IN_PROGRESS"))

    page.introduce_batch()
    _settle(application)

    assert issue.load("blk-9").doc_id == "doc-1"
    assert "итога пока нет" in page.batches_hint.text()


def test_отказ_системы_показан_с_причиной(application, tmp_path, stub, monkeypatch):
    from app.core.marking import introduce

    page, seen = _intro_page(application, tmp_path, stub, monkeypatch)
    seen["result"] = introduce.Sent("doc-1", introduce.DocStatus(
        "CHECKED_NOT_OK", ("Код уже в обороте",)))

    page.introduce_batch()
    _settle(application)

    assert "Код уже в обороте" in page.batches_hint.text()


def test_ошибка_отправки_не_помечает_блок_отправленным(application, tmp_path, stub,
                                                       monkeypatch):
    from app.core.marking import issue
    from app.ui import marking_page

    page, seen = _intro_page(application, tmp_path, stub, monkeypatch)

    def failing(*args, **kwargs):
        raise MarkingError("Подпись невалидна")

    monkeypatch.setattr(marking_page.introduce_module, "send", failing)

    page.introduce_batch()
    _settle(application)

    assert issue.load("blk-9").doc_id == ""
    assert "Подпись невалидна" in page.batches_hint.text()


def test_сохранение_файла_ничего_не_отправляет(application, tmp_path, stub, monkeypatch):
    page, seen = _intro_page(application, tmp_path, stub, monkeypatch, action="file")

    page.introduce_batch()
    _settle(application)

    assert seen["sent"] == []


def test_без_входа_отправка_запрещена_с_причиной(application, tmp_path, stub,
                                                monkeypatch):
    page, seen = _intro_page(application, tmp_path, stub, monkeypatch, action="file",
                             signed=False)

    page.introduce_batch()

    assert "не выполнен вход" in seen["dialogs"][0]["problem"]["remark"]
    assert "не выполнен вход" in seen["dialogs"][0]["problem"]["ostatky"]


def test_группа_без_поддержки_отправки_называет_причину(application, tmp_path, stub,
                                                       monkeypatch):
    page, seen = _intro_page(application, tmp_path, stub, monkeypatch, action="file",
                             group="perfumery")

    page.introduce_batch()

    problems = seen["dialogs"][0]["problem"]
    # Остатки для этой группы не подключены, а перемаркировка — общий документ.
    assert "не подключена" in problems["ostatky"]
    assert problems["remark"] == ""


def test_блок_из_другого_контура_не_отправляется(application, tmp_path, stub,
                                                monkeypatch):
    page, seen = _intro_page(application, tmp_path, stub, monkeypatch, action="file")
    page.contour_box.setCurrentIndex(
        page.contour_box.findData(Contour.PRODUCTION.value))

    page.batches.selectRow(0)
    page.introduce_batch()

    assert "в контуре" in seen["dialogs"][0]["problem"]["remark"]


def test_инн_берётся_из_выполненного_входа(application, tmp_path, stub, monkeypatch):
    page, seen = _intro_page(application, tmp_path, stub, monkeypatch, action="file")

    page.introduce_batch()

    assert seen["dialogs"][0]["inn"] == "2536000000"


def test_статус_документа_обновляется_по_запросу(application, tmp_path, stub,
                                                monkeypatch):
    from app.core.marking import introduce, issue
    from app.ui import marking_page

    page, seen = _intro_page(application, tmp_path, stub, monkeypatch)
    batch = issue.load("blk-9")
    issue.mark_sent(batch, "doc-1", 5, "IN_PROGRESS")
    page.reload_batches(select="blk-9")
    asked: list = []

    def status(doc_id, group, contour):
        asked.append((doc_id, group))
        return introduce.DocStatus("CHECKED_OK")

    monkeypatch.setattr(marking_page.introduce_module, "status", status)

    page.refresh_document_status()
    _settle(application)

    assert asked == [("doc-1", "lp")]
    assert issue.load("blk-9").doc_status == "CHECKED_OK"


def test_у_блока_без_документа_статус_не_спрашивается(application, tmp_path, stub,
                                                     monkeypatch):
    from app.ui import marking_page

    page, _ = _intro_page(application, tmp_path, stub, monkeypatch)
    asked: list = []
    monkeypatch.setattr(marking_page.introduce_module, "status",
                        lambda *args: asked.append(args))

    page.refresh_document_status()
    _settle(application)

    assert asked == []


def test_статус_не_получен_это_не_отказ_и_повторять_не_нужно(application, tmp_path, stub,
                                                           monkeypatch):
    """Документ создан; сказать «не выполнен» значило бы толкнуть к повторной отправке."""
    from app.core.marking import introduce, issue

    page, seen = _intro_page(application, tmp_path, stub, monkeypatch)
    seen["result"] = introduce.Sent("doc-1", introduce.DocStatus(
        errors=("статус не получен: Метод с указанным URL не найден",)))

    page.introduce_batch()
    _settle(application)

    saved = issue.load("blk-9")
    assert saved.doc_id == "doc-1"
    # Текст ошибки в колонку статуса не попадает — только настоящий статус.
    assert saved.doc_status == ""
    assert "не отправляйте повторно" in page.batches_hint.text().lower()
    assert "не выполнен" not in page.batches_hint.text().lower()


# --- закрытие заказа и удаление блоков ---------------------------------------------------

@pytest.fixture
def asked(monkeypatch):
    """Вопрос подтверждения вместо окна: что спросили и что ответили."""
    from PySide6.QtWidgets import QMessageBox

    from app.ui import marking_page

    box: dict = {"yes": True, "text": [], "buttons": []}

    class Fake:
        Icon = QMessageBox.Icon
        ButtonRole = QMessageBox.ButtonRole

        def __init__(self, parent=None):
            self._yes = None

        def setWindowTitle(self, text):
            pass

        def setIcon(self, icon):
            pass

        def setText(self, text):
            box["text"].append(text)

        def setInformativeText(self, text):
            box["text"].append(text)

        def addButton(self, text, role):
            button = object()
            box["buttons"].append(text)
            if role == QMessageBox.ButtonRole.DestructiveRole:
                self._yes = button
            return button

        def setDefaultButton(self, button):
            pass

        def exec(self):
            return 0

        def clickedButton(self):
            return self._yes if box["yes"] else None

    monkeypatch.setattr(marking_page, "QMessageBox", Fake)
    return box


def _close_page(application, tmp_path, stub, monkeypatch, *, order=None):
    """Вкладка с одним заказом и подменённым закрытием в СУЗ."""
    from app.ui import marking_page

    _, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    state["orders"] = [order or _order(buffers=[Buffer(
        gtin="04601234567893", left=0, passed=10, total=10, status="EXHAUSTED")])]
    closed: list = []

    def close_suz_order(credentials, order_id, contour=Contour.SANDBOX,
                        thumbprint="", inn=""):
        closed.append((order_id, contour, thumbprint))
        # После закрытия СУЗ показывает заказ закрытым.
        state["orders"] = [_order(status="CLOSED", buffers=state["orders"][0].buffers)]
        return None, credentials.stripped()

    monkeypatch.setattr(marking_page.service, "close_suz_order", close_suz_order)
    page = _page(application, tmp_path)
    _settle(application)
    return page, closed


def test_заказ_с_полученными_кодами_закрывается_после_подтверждения(
        application, tmp_path, stub, monkeypatch, asked):
    from app.core.marking import issue

    issue.save(issue.Batch(id="a", order_id="3a8f-0001", gtin="04601234567893",
                           codes=[PERFUME] * 10))
    page, closed = _close_page(application, tmp_path, stub, monkeypatch)
    assert not page.close_order_button.isEnabled()      # заказ не выбран

    page.orders.selectRow(0)
    assert page.close_order_button.isEnabled()
    page.close_order()
    _settle(application)

    assert closed == [("3a8f-0001", Contour.SANDBOX, "ААББ")]
    # Список перечитан: заказ закрыт, и кнопка больше не нужна.
    assert page.orders.item(0, 2).text() == "Закрыт"
    page.orders.selectRow(0)
    assert not page.close_order_button.isEnabled()
    # Всё выданное лежит на диске — предупреждения о потере нет.
    assert not any("ВНИМАНИЕ" in text for text in asked["text"])


def test_отказ_в_подтверждении_ничего_не_закрывает(application, tmp_path, stub,
                                                   monkeypatch, asked):
    asked["yes"] = False
    page, closed = _close_page(application, tmp_path, stub, monkeypatch)
    page.orders.selectRow(0)

    page.close_order()
    _settle(application)

    assert closed == []


def test_закрытие_предупреждает_о_несохранённых_и_неполученных_кодах(
        application, tmp_path, stub, monkeypatch, asked):
    """Блок, которого нет на диске, после закрытия не вернуть — об этом спрашивают заранее."""
    page, closed = _close_page(application, tmp_path, stub, monkeypatch,
                               order=_order())     # получено 750, в буфере ещё 250
    page.orders.selectRow(0)

    page.close_order()
    _settle(application)

    text = "\n".join(asked["text"])
    assert "сохранено 0 из 750" in text
    assert "ещё 250 кодов" in text
    assert closed                                   # решение за человеком


def test_нетронутый_заказ_закрыть_нельзя(application, tmp_path, stub, monkeypatch, asked):
    page, closed = _close_page(
        application, tmp_path, stub, monkeypatch,
        order=_order(buffers=[Buffer(gtin="1", left=500, total=500, passed=0)]))
    page.orders.selectRow(0)

    assert not page.close_order_button.isEnabled()
    page.close_order()
    _settle(application)

    assert closed == [] and asked["text"] == []


def test_отказ_суз_при_закрытии_не_делает_заказ_закрытым(application, tmp_path, stub, monkeypatch,
                                          asked):
    from app.ui import marking_page

    page, closed = _close_page(application, tmp_path, stub, monkeypatch)

    def refuse(*args, **kwargs):
        raise MarkingError("Заказ уже закрыт")

    monkeypatch.setattr(marking_page.service, "close_suz_order", refuse)
    page.orders.selectRow(0)
    page.close_order()
    _settle(application)

    # Ошибка не делает вид, что заказ закрыт: список перечитан, он по-прежнему
    # открыт, и закрыть его можно снова.
    assert page.orders.item(0, 2).text() == "Готов"
    page.orders.selectRow(0)
    assert page.close_order_button.isEnabled()
    assert not page._busy


def test_блок_кодов_удаляется_после_подтверждения_и_файл_остаётся(
        application, tmp_path, stub, monkeypatch, asked):
    from app.core.marking import issue

    issue.save(issue.Batch(id="old", gtin="04601234567893", name="Духи",
                           codes=[PERFUME] * 5, printed=2,
                           created="2026-10-01T09:00:00"))
    page = _page(application, tmp_path)
    assert not page.batch_delete_button.isEnabled()      # блок не выбран

    page.batches.selectRow(0)
    assert page.batch_delete_button.isEnabled()
    page.delete_batch()

    assert page.batches.rowCount() == 0
    assert issue.saved() == []
    assert os.path.exists(os.path.join(issue.folder(), issue.TRASH, "old.json"))
    # Человеку сказали, сколько останется ненапечатанным.
    assert "Не напечатано ещё 3" in "\n".join(asked["text"])


def test_отказ_в_подтверждении_оставляет_блок(application, tmp_path, stub, monkeypatch,
                                              asked):
    from app.core.marking import issue

    asked["yes"] = False
    issue.save(issue.Batch(id="keep", gtin="04601234567893", codes=[PERFUME] * 2,
                           created="2026-10-01T09:00:00"))
    page = _page(application, tmp_path)
    page.batches.selectRow(0)

    page.delete_batch()

    assert page.batches.rowCount() == 1
    assert [batch.id for batch in issue.saved()] == ["keep"]


def _many_page(application, tmp_path, stub, monkeypatch, orders_list, closing):
    """Вкладка с несколькими заказами; `closing(order_id)` решает судьбу закрытия."""
    from app.ui import marking_page

    _, state = stub
    state["suz"] = {Contour.SANDBOX: SUZ}
    state["orders"] = list(orders_list)
    closed: list = []

    def close_suz_order(credentials, order_id, contour=Contour.SANDBOX,
                        thumbprint="", inn=""):
        closed.append(order_id)
        closing(order_id)
        state["orders"] = [_order(id=o.id, status="CLOSED", buffers=o.buffers)
                           if o.id == order_id else o for o in state["orders"]]
        return None, credentials.stripped()

    monkeypatch.setattr(marking_page.service, "close_suz_order", close_suz_order)
    page = _page(application, tmp_path)
    _settle(application)
    return page, closed


def _done(order_id: str) -> Order:
    return _order(id=order_id, buffers=[Buffer(gtin="1", left=0, passed=10, total=10)])


def test_несколько_заказов_закрываются_одним_подтверждением(
        application, tmp_path, stub, monkeypatch, asked):
    page, closed = _many_page(
        application, tmp_path, stub, monkeypatch,
        [_done("o1"), _done("o2"), _done("o3")], lambda order_id: None)

    page.orders.selectAll()
    assert page.close_order_button.isEnabled()
    assert "(3)" in page.close_order_button.text()
    page.close_order()
    _settle(application)

    assert closed == ["o1", "o2", "o3"]
    assert [page.orders.item(row, 2).text() for row in range(3)] == ["Закрыт"] * 3
    # Один вопрос на всех, и в нём видны все номера.
    assert asked["buttons"].count("Отмена") == 1
    text = "\n".join(asked["text"])
    assert all(f"· o{n} — получено 10" in text for n in (1, 2, 3))


def test_отказ_по_одному_заказу_не_останавливает_остальные(
        application, tmp_path, stub, monkeypatch, asked):
    def closing(order_id):
        if order_id == "o2":
            raise MarkingError("Заказ уже закрыт")

    page, closed = _many_page(
        application, tmp_path, stub, monkeypatch,
        [_done("o1"), _done("o2"), _done("o3")], closing)

    page.orders.selectAll()
    page.close_order()
    _settle(application)

    assert closed == ["o1", "o2", "o3"]
    states = {page.orders.item(row, 0).text(): page.orders.item(row, 2).text()
              for row in range(3)}
    # Закрыты два, а отказавший остался открытым — его можно закрыть снова.
    assert states == {"o1": "Закрыт", "o2": "Готов", "o3": "Закрыт"}
    assert not page._busy


def test_в_пачке_закрываются_только_годные_заказы(application, tmp_path, stub,
                                                   monkeypatch, asked):
    untouched = _order(id="o2", buffers=[Buffer(gtin="1", left=500, total=500)])
    page, closed = _many_page(
        application, tmp_path, stub, monkeypatch,
        [_done("o1"), untouched, _order(id="o3", status="CLOSED",
                                        buffers=_done("o3").buffers)],
        lambda order_id: None)

    page.orders.selectAll()
    assert page.close_order_button.text() == "Закрыть заказ…"   # годный один
    page.close_order()
    _settle(application)

    assert closed == ["o1"]
    assert "Пропущено 2" in "\n".join(asked["text"])


def test_коды_получают_по_одному_заказу(application, tmp_path, stub, monkeypatch):
    page, seen = _issue_page(application, tmp_path, stub, monkeypatch)
    _, state = stub
    state["orders"] = [_order(), _order(id="3a8f-0002")]
    page.reload_orders()
    _settle(application)
    page.orders.selectAll()

    page.fetch_codes()
    _settle(application)

    assert seen["asked"] == [] and seen["fetched"] == []


# --- выгрузка из сверки в «Срок годности» --------------------------------------------------

def _reconciled_page(application, tmp_path, stub, *, signed=True):
    """Вкладка со сверкой документа и подменённым ответом «Честного ЗНАКа»."""
    from app.core.marking import reconcile, upd

    from tests.test_marking_reconcile import DOCUMENT, FIRST, SECOND

    path = tmp_path / "ON_NSCHFDOPPR_2BM-1.xml"
    path.write_bytes(DOCUMENT.encode("cp1251"))
    _, state = stub
    state["signed"] = signed
    page = _page(application, tmp_path)
    page._upd = upd.read(str(path))
    page._session = reconcile.Reconciliation(page._upd)
    page._sync_reconcile()
    asked: list = []
    page.expiry_tab._lookup_many = lambda codes: asked.append(list(codes)) or {}
    return page, asked, (FIRST, SECOND)


def test_без_сканов_выгружать_в_сроки_нечего(application, tmp_path, stub):
    page, asked, _ = _reconciled_page(application, tmp_path, stub)

    assert not page.upd_expiry_button.isEnabled()
    page.codes_to_expiry()

    assert page.expiry_tab.entries == [] and asked == []


def test_отсканированное_при_сверке_уходит_в_срок_годности(application, tmp_path, stub):
    from app.ui.marking_page import EXPIRY_TAB

    page, asked, (first, second) = _reconciled_page(application, tmp_path, stub)
    page._session.scan(first)
    page._session.scan(second)
    page._sync_reconcile()
    assert page.upd_expiry_button.isEnabled()

    page.codes_to_expiry()
    _settle(application)

    assert page.tabs.currentIndex() == EXPIRY_TAB
    assert [entry.card.kiz for entry in page.expiry_tab.entries] == [first, second]
    # Срок спрошен одной пачкой, а не по запросу на код.
    assert asked == [[first, second]]


def test_без_входа_срок_не_выгружается(application, tmp_path, stub):
    from app.ui.marking_page import RECONCILE_TAB

    page, asked, (first, _) = _reconciled_page(application, tmp_path, stub, signed=False)
    page.tabs.setCurrentIndex(RECONCILE_TAB)
    page._session.scan(first)
    page._sync_reconcile()

    page.codes_to_expiry()
    _settle(application)

    assert page.expiry_tab.entries == [] and asked == []
    assert page.tabs.currentIndex() == RECONCILE_TAB


def test_повторная_выгрузка_не_дублирует_журнал(application, tmp_path, stub):
    page, asked, (first, second) = _reconciled_page(application, tmp_path, stub)
    page._session.scan(first)
    page._sync_reconcile()

    page.codes_to_expiry()
    _settle(application)
    page._session.scan(second)
    page.codes_to_expiry()
    _settle(application)

    assert [entry.card.kiz for entry in page.expiry_tab.entries] == [first, second]
    assert asked == [[first], [second]]


# --- раскладка вкладки «Заказ кодов» ---------------------------------------------------------

def test_заказ_кодов_не_растягивается_до_высоты_других_вкладок(application, tmp_path,
                                                              stub):
    """Страница лежит в прокручиваемой области: раньше она брала высоту самой
    высокой вкладки, и список заказов уезжал под нижний край."""
    from app.ui.marking_page import ORDER_TAB

    page = _page(application, tmp_path)
    page.tabs.setCurrentIndex(ORDER_TAB)
    application.processEvents()

    check_tab = page.tabs.widget(0).minimumSizeHint().height()
    assert page.tabs.sizeHint().height() < check_tab
    assert not page.tabs.hasHeightForWidth()
    # Скрытые вкладки высоту не просят.
    assert page.tabs.widget(0).sizePolicy().verticalPolicy().name == "Ignored"
    assert page.tabs.widget(ORDER_TAB).sizePolicy().verticalPolicy().name != "Ignored"


def test_настроенное_соединение_свёрнуто_а_ненастроенное_открыто(application, tmp_path,
                                                                 stub):
    _, state = stub
    empty = _page(application, tmp_path)
    assert not empty.suz_form.isHidden()

    state["suz"] = {Contour.SANDBOX: SUZ}
    configured = _page(application, tmp_path)
    assert configured.suz_form.isHidden()
    # Состояние проверки остаётся на виду, а поля раскрываются кнопкой.
    configured._toggle_suz()
    assert not configured.suz_form.isHidden()
    configured._toggle_suz()
    assert configured.suz_form.isHidden()


def test_вкладки_просят_предпочтительную_высоту_открытой_страницы(application, tmp_path,
                                                                 stub):
    """Не минимум: у проверки кодов и сверки он занижен, и плитки налезали на соседей."""
    page = _page(application, tmp_path)

    for index in range(page.tabs.count() - 1):
        page.tabs.setCurrentIndex(index)
        application.processEvents()
        current = page.tabs.widget(index)
        assert page.tabs.sizeHint().height() >= current.sizeHint().height()
        assert page.tabs.sizeHint().height() >= current.minimumSizeHint().height()


def test_шапки_карточек_заказа_не_раздувают_вкладку(application, tmp_path, stub):
    """Второстепенное — значком с подсказкой, иначе окно минимальной ширины
    получало горизонтальную прокрутку."""
    page = _page(application, tmp_path)

    for button in (page.orders_button,):
        assert button.text() == "" and button.toolTip()
    assert page.recover_button.text() == "Восстановить"
    # Сохранить и забыть — под полями реквизитов, а не в шапке карточки.
    assert page.suz_form.isAncestorOf(page.suz_save_button)
    assert page.suz_form.isAncestorOf(page.suz_forget_button)
    # «В срок годности» — в шапке списка позиций, а не среди кнопок документа.
    assert page.progress.parentWidget().isAncestorOf(page.upd_expiry_button)
