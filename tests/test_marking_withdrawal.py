"""Вывод из оборота: документ `LK_RECEIPT` (JSON) и вкладка со сканером.

Состав документа снят с ПРИНТМАРКИ (`marklib.dll`), и тесты держат его как есть:
порядок ключей, место осуществления деятельности (КПП и ФИАС), код без
криптохвоста, причина и первичный документ. Сеть и КриптоПро
подменяются; главное, что проверяется, — необратимое не уходит молча: без
подтверждения, без входа, без сертификата и повторно тем же набором.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.marking import introduce, transport, withdrawal
from app.core.marking.codes import GS
from app.core.marking.models import Contour, OperationKind, OperationStatus
from app.core.marking.withdrawal import Withdrawal

GTIN = "04680962315457"
FIRST = f"01{GTIN}215hsFUtaRDI3mf{GS}91EE11{GS}92dGVzdGRhdGE="
SECOND = f"01{GTIN}215abcDEFxyZ01{GS}91EE11{GS}92dGVzdGRhdGE="
FIRST_KI = f"01{GTIN}215hsFUtaRDI3mf"
SECOND_KI = f"01{GTIN}215abcDEFxyZ01"
FIAS = "0d7b4c1e-5a2f-4c1b-9d3e-1f2a3b4c5d6e"


def _body(document: Withdrawal) -> dict:
    return json.loads(document.render())


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    transport.limiter.reset()
    return tmp_path


def _document(**changes) -> Withdrawal:
    values = dict(inn="250101802801", cause="RETAIL", date="2026-10-06",
                  codes=(FIRST, SECOND), doc_type="RECEIPT", doc_number="145",
                  doc_date="2026-10-06", price="1490", fias_id=FIAS)
    values.update(changes)
    return Withdrawal(**values)


# --- причины ------------------------------------------------------------------------

def test_причины_названы_как_в_принтмарки_и_в_том_же_порядке():
    causes = list(withdrawal.WITHDRAWAL_CAUSES)

    assert causes[:8] == ["PACKING", "RETAIL", "BY_SAMPLES", "DISTANCE", "OWN_USE",
                          "PRODUCTION_USE", "VENDING", "BEYOND_EEC_EXPORT"]
    assert causes[-1] == "OTHER"
    assert withdrawal.cause_title("RETAIL") == "Розничная продажа — RETAIL"
    assert withdrawal.cause_title("НОВАЯ") == "НОВАЯ"


# --- состав документа ---------------------------------------------------------------

def test_документ_собран_по_шаблону_принтмарки():
    text = _document().render()

    assert json.loads(text) == {
        "inn": "250101802801",
        "action": "RETAIL",
        "action_date": "2026-10-06",
        "document_type": "RECEIPT",
        "document_number": "145",
        "document_date": "2026-10-06",
        "fias_id": FIAS,
        "products": [{"cis": FIRST_KI, "product_cost": 149000},
                     {"cis": SECOND_KI, "product_cost": 149000}],
    }
    # Порядок ключей — как в шаблоне ПРИНТМАРКИ.
    assert list(json.loads(text)) == [
        "inn", "action", "action_date", "document_type", "document_number",
        "document_date", "fias_id", "products"]


# --- место осуществления деятельности ------------------------------------------------

def test_без_фиас_документ_не_собирается_система_не_найдёт_место():
    problems = _document(fias_id="").problems

    assert any("ФИАС" in problem for problem in problems)
    with pytest.raises(introduce.IntroduceProblem, match="ФИАС"):
        _document(fias_id="").render()


def test_фиас_должен_быть_идентификатором():
    assert any("ФИАС" in problem
               for problem in _document(fias_id="улица Ленина, 1").problems)


@pytest.mark.parametrize("pasted", [
    FIAS, " " + FIAS + " ", "{" + FIAS + "}", "(" + FIAS + ")",
    chr(0xFEFF) + FIAS, FIAS.upper(), FIAS.replace("-", ""),
    chr(0x200B) + FIAS + chr(0xA0),
])
def test_фиас_принимается_как_его_копируют_из_кабинета(pasted):
    document = _document(fias_id=pasted)

    assert not any("ФИАС" in problem for problem in document.problems)
    assert _body(document)["fias_id"] == FIAS


def test_настоящий_фиас_из_кабинета_проходит():
    real = "36005e10-ebf6-4a46-8e80-5f6e1f9ad488"

    assert _body(_document(fias_id=real))["fias_id"] == real


def test_негодный_фиас_показан_в_сообщении():
    problems = _document(fias_id="не то").problems

    assert any("'не то'" in problem for problem in problems)


def test_поле_фиас_не_режет_вставленное_и_чистит_его(world):
    tab, _ = world

    tab.fias_edit.setText("{" + FIAS.upper() + "} ")
    tab.fias_edit.editingFinished.emit()

    assert tab.fias_edit.text() == FIAS
    assert tab.fias_edit.maxLength() > 36


def test_ип_отправляет_без_кпп_а_организация_с_кпп():
    # ИП (12 цифр) КПП не имеет — ключа в документе нет, как в сообщении системы
    # «КПП null».
    assert "kpp" not in _body(_document())

    organisation = _document(inn="2540098957", kpp="254001001")
    assert _body(organisation)["kpp"] == "254001001"
    assert list(_body(organisation)).index("kpp") < list(_body(organisation)).index(
        "fias_id")
    assert any("КПП" in problem
               for problem in _document(inn="2540098957", kpp="").problems)
    assert any("9 цифр" in problem for problem in _document(kpp="12").problems)


# --- цена за единицу ----------------------------------------------------------------

def test_без_цены_розничный_вывод_не_собирается():
    problems = _document(price="").problems

    assert any("цена за единицу" in problem and "Розничная продажа" in problem
               for problem in problems)
    with pytest.raises(introduce.IntroduceProblem, match="цена за единицу"):
        _document(price="").render()


@pytest.mark.parametrize("text, expected", [
    ("1490", 149000), ("99,90", 9990), ("99.9", 9990), (" 1 490 ", 149000),
    ("0", 0), ("0,005", 1), ("abc", None), ("-5", None), ("", None), ("inf", None),
])
def test_цена_из_рублей_в_копейки(text, expected):
    assert withdrawal.kopecks(text) == expected


def test_у_кода_может_быть_своя_цена():
    products = _body(_document(price="100", code_prices=("", "99,90")))["products"]

    assert [item["product_cost"] for item in products] == [10000, 9990]


def test_кривая_цена_называется():
    assert any("число в рублях" in problem
               for problem in _document(price="дорого").problems)


@pytest.mark.parametrize("cause", sorted(withdrawal.WITHDRAWAL_CAUSES))
def test_цена_нужна_только_тем_причинам_где_её_пишет_принтмарки(cause):
    document = _document(cause=cause, cause_other="словами", price="")

    if cause in withdrawal.COST_CAUSES:
        assert any("цена за единицу" in problem for problem in document.problems)
    else:
        assert not any("цена" in problem for problem in document.problems)
        assert "product_cost" not in document.render()


def test_цена_для_причины_без_цены_в_документ_не_попадает():
    assert "product_cost" not in _document(cause="LOSS", price="1490").render()


def test_в_документ_идёт_код_идентификации_без_криптохвоста():
    text = _document().render()

    assert "91EE11" not in text and "92dGVz" not in text and GS not in text


def test_причина_другое_требует_описания_и_пишется_в_свой_ключ():
    assert any("«Другое»" in problem
               for problem in _document(cause="OTHER").problems)

    body = _body(_document(cause="OTHER", cause_other="Подарок сотруднику"))

    assert body["action"] == "OTHER"
    assert body["withdrawal_type_other"] == "Подарок сотруднику"
    # Описание причины стоит между причиной и датой, как в шаблоне.
    keys = list(body)
    assert keys.index("action") < keys.index("withdrawal_type_other") < keys.index(
        "action_date")


def test_описание_чужой_причины_в_документ_не_попадает():
    text = _document(cause_other="лишнее").render()

    assert "withdrawal_type_other" not in text and "лишнее" not in text


def test_иной_первичный_документ_требует_наименования():
    other = _document(doc_type="OTHER", doc_number="7", doc_date="2026-10-05")
    assert any("наименование" in problem for problem in other.problems)

    body = _body(_document(doc_type="OTHER", doc_number="7", doc_date="2026-10-05",
                           doc_name="Акт списания"))

    assert body["primary_document_custom_name"] == "Акт списания"


def test_первичный_документ_заполняется_целиком_или_не_заполняется():
    assert any("целиком" in problem for problem in _document(doc_number="").problems)

    body = _body(_document(doc_type="", doc_number="", doc_date=""))

    assert not any(key.startswith(("document_", "primary_document")) for key in body)


def test_спецсимволы_и_кириллица_не_ломают_json():
    body = _body(_document(cause="OTHER", cause_other='Брак <"А"> & Ко'))

    assert body["withdrawal_type_other"] == 'Брак <"А"> & Ко'


def test_документ_без_кодов_и_с_мусором_не_собирается():
    with pytest.raises(introduce.IntroduceProblem, match="нет ни одного кода"):
        _document(codes=()).render()
    with pytest.raises(introduce.IntroduceProblem, match="не разбираются"):
        _document(codes=(FIRST, "МУСОР")).render()


def test_один_экземпляр_с_разным_хвостом_это_дубль():
    again = f"01{GTIN}215hsFUtaRDI3mf{GS}91EE11{GS}92другой=="

    problems = _document(codes=(FIRST, again)).problems

    assert any("одинаковые коды" in problem for problem in problems)


@pytest.mark.parametrize("changes, expected", [
    ({"inn": "123"}, "ИНН участника"),
    ({"cause": "ВЫДУМАНА"}, "причина"),
    ({"date": "06.10.2026"}, "ГГГГ-ММ-ДД"),
    ({"doc_type": "ЧЕК"}, "неизвестный вид"),
    ({"buyer_inn": "12"}, "ИНН покупателя"),
])
def test_негодные_реквизиты_называются(changes, expected):
    assert any(expected in problem for problem in _document(**changes).problems)


def test_госконтракт_несёт_номер_контракта():
    body = _body(_document(cause="STATE_CONTRACT", state_contract_id="К-77"))

    assert body["state_contract_id"] == "К-77"
    assert "state_contract_id" not in _body(_document())


def test_ключи_покупателя_и_контракта_стоят_где_в_шаблоне():
    body = _body(_document(cause="STATE_CONTRACT", state_contract_id="К-77",
                           buyer_inn="7701234567"))

    keys = list(body)
    assert keys[:2] == ["inn", "buyer_inn"]
    assert keys.index("fias_id") < keys.index("state_contract_id") < keys.index("products")


# --- отправка -----------------------------------------------------------------------

@pytest.fixture
def served(monkeypatch):
    calls: list = []
    answers: list = []

    def send(prepared, tolerate=()):
        calls.append(prepared)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(transport, "_send", send)
    signed: list = []
    monkeypatch.setattr(introduce.session, "authorized", lambda call: call("TOKEN"))
    monkeypatch.setattr(
        introduce.crypto, "sign",
        lambda data, thumbprint, detached=False: signed.append((data, detached)) or "ПОДПИСЬ")
    return calls, answers, signed


def test_вывод_уходит_единым_методом_типом_lk_receipt_json(served):
    calls, answers, signed = served
    answers.append("11111111-2222-3333-4444-555555555555")

    doc_id = introduce.submit(_document(), "ААББ", "lp", Contour.SANDBOX)

    assert doc_id == "11111111-2222-3333-4444-555555555555"
    request = calls[0]
    assert "/lk/documents/create" in request.full_url and "pg=lp" in request.full_url
    body = json.loads(request.data)
    assert set(body) == {"document_format", "product_document", "type", "signature"}
    assert body["document_format"] == "MANUAL" and body["type"] == "LK_RECEIPT"
    sent = base64.b64decode(body["product_document"]).decode("utf-8")
    # Подписано ровно то, что ушло, и подпись откреплённая.
    assert signed == [(sent, True)] and sent == _document().render()


def test_без_товарной_группы_вывод_не_отправляется_и_не_подписывается(served):
    calls, _, signed = served

    with pytest.raises(introduce.IntroduceProblem, match="товарная группа|товарной группы"):
        introduce.submit(_document(), "ААББ", "", Contour.SANDBOX)

    assert calls == [] and signed == []


def test_отправка_вывода_не_повторяется_при_обрыве_связи(served):
    calls, answers, _ = served
    answers.append(transport.Offline("сети нет"))

    with pytest.raises(introduce.IntroduceUnknown, match="личном кабинете"):
        introduce.submit(_document(), "ААББ", "lp")

    assert len(calls) == 1


# --- вкладка ------------------------------------------------------------------------

@pytest.fixture(scope="module")
def application():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def world(application, tmp_path, monkeypatch):
    """Вкладка с подменённым входом, отправкой и окном подтверждения."""
    from PySide6.QtWidgets import QMessageBox

    from app.core.marking import service
    from app.core.settings import AppSettings
    from app.ui.widgets import withdrawal_tab as module

    state = {"signed": True, "yes": True, "text": [], "sent": [], "answer": None,
             "thumbprint": "ААББ", "contour": Contour.SANDBOX, "told": []}

    class Session:
        contour = Contour.SANDBOX
        inn = "250101802801"

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
            state["text"].append(text)

        def setInformativeText(self, text):
            state["text"].append(text)

        def addButton(self, text, role):
            button = object()
            if role == QMessageBox.ButtonRole.DestructiveRole:
                self._yes = button
            return button

        def setDefaultButton(self, button):
            pass

        def exec(self):
            return 0

        def clickedButton(self):
            return self._yes if state["yes"] else None

    def send(document, thumbprint, group, contour, created):
        state["sent"].append((document, thumbprint, group, contour))
        answer = state["answer"]
        if isinstance(answer, Exception):
            raise answer
        created("док-1")
        return "sent", answer

    monkeypatch.setattr(module, "QMessageBox", Fake)
    monkeypatch.setattr(module, "_send", send)
    monkeypatch.setattr(service, "signed_in", lambda: state["signed"])
    monkeypatch.setattr(service, "current", lambda: Session)
    tab = module.WithdrawalTab(
        AppSettings(_path=tmp_path / "settings.json"),
        notify=lambda text, kind: state["told"].append((text, kind)),
        contour=lambda: state["contour"], thumbprint=lambda: state["thumbprint"],
        inn=lambda: "250101802801")
    tab.prefill_inn()
    tab.doc_number_edit.setText("145")
    tab.fias_edit.setText(FIAS)
    # Наименования ищутся у системы — в тестах не ищутся; цену, как пользователь,
    # вписывают в строку каждого нового кода.
    monkeypatch.setattr(module, "_look_up_names", lambda *_: {})
    add = tab.add_code

    def add_priced(text, **kwargs):
        result = add(text, **kwargs)
        if result == "added":
            tab.table.item(tab.table.rowCount() - 1, module.PRICE_COLUMN).setText("1490")
        return result

    tab.add_code = add_priced
    return tab, state


def _settle(application) -> None:
    from PySide6.QtCore import QThreadPool

    for _ in range(6):
        QThreadPool.globalInstance().waitForDone(3000)
        application.processEvents()


def _done_status(code="CHECKED_OK", errors=()):
    return introduce.Sent("док-1", introduce.DocStatus(code=code, errors=tuple(errors)))


def test_отсканированные_марки_попадают_в_список_с_ответом_на_каждую(world):
    tab, _ = world

    assert tab.add_code(FIRST) == "added"
    assert tab.add_code(SECOND) == "added"

    assert tab.codes == [FIRST, SECOND]
    assert tab.table.rowCount() == 2
    assert tab.table.item(0, 1).text() == GTIN
    assert tab.table.item(0, 3).text() == FIRST_KI
    assert "Принято" in tab.verdict_label.text()
    assert tab.tile_codes._value.text() == "2"


def test_сканер_вводит_код_в_поле_и_жмёт_enter(world):
    tab, _ = world

    tab.scan_edit.setText(FIRST)
    tab.scan_edit.returnPressed.emit()

    assert tab.codes == [FIRST] and tab.scan_edit.text() == ""


def test_повторный_скан_и_мусор_не_попадают_в_список(world):
    tab, _ = world
    tab.add_code(FIRST)

    again = f"01{GTIN}215hsFUtaRDI3mf{GS}91EE11{GS}92другой=="
    assert tab.add_code(again) == "repeat"
    assert tab.add_code("МУСОР") == "broken"

    assert tab.codes == [FIRST]
    assert tab.tile_repeats._value.text() == "1" and tab.tile_broken._value.text() == "1"
    assert "Не принято" in tab.verdict_label.text()


def test_вставка_списком_считает_итог(world):
    tab, _ = world

    added, repeats, broken = tab.add_many(f"{FIRST}\n{SECOND}\n{FIRST}\nмусор\n")

    assert (added, repeats, broken) == (2, 1, 1)
    assert "Добавлено кодов: 2" in tab.verdict_label.text()


def test_убрать_выбранные_и_очистить(world):
    tab, _ = world
    tab.add_many(f"{FIRST}\n{SECOND}")

    tab.table.selectRow(0)
    tab.remove_selected()

    assert tab.codes == [SECOND] and tab.table.item(0, 0).text() == "1"
    # Убранный код можно отсканировать снова: он больше не «повтор».
    assert tab.add_code(FIRST) == "added"
    tab.clear()
    assert tab.codes == [] and tab.table.rowCount() == 0


def test_документ_собирается_из_того_что_на_экране(world):
    tab, _ = world
    tab.add_many(f"{FIRST}\n{SECOND}")

    document = tab.document()

    assert document.inn == "250101802801" and document.cause == "RETAIL"
    assert document.codes == (FIRST, SECOND)
    assert (document.doc_type, document.doc_number) == ("RECEIPT", "145")
    assert tab.group == "lp"


def test_без_кодов_отправлять_нечего(world):
    tab, _ = world

    assert not tab.send_button.isEnabled() and not tab.file_save_button.isEnabled()


def test_причина_другое_открывает_поле_описания(world):
    tab, _ = world
    assert tab.cause_other_edit.isHidden()

    tab.cause_box.setCurrentIndex(tab.cause_box.findData("OTHER"))
    assert not tab.cause_other_edit.isHidden()

    tab.doc_type_box.setCurrentIndex(tab.doc_type_box.findData("OTHER"))
    assert not tab.doc_name_edit.isHidden()
    tab.doc_type_box.setCurrentIndex(0)
    assert not tab.doc_number_edit.isEnabled()


@pytest.mark.parametrize("break_it, expected", [
    (lambda tab, state: state.update(signed=False), "войдите"),
    (lambda tab, state: state.update(thumbprint=""), "выберите сертификат"),
    (lambda tab, state: state.update(contour=Contour.PRODUCTION), "другом контуре"),
    (lambda tab, state: tab.inn_edit.setText("12"), "ИНН участника"),
])
def test_без_условий_отправки_ничего_не_уходит_и_не_записывается(
        world, break_it, expected):
    from app.core.marking import service

    tab, state = world
    tab.add_many(f"{FIRST}\n{SECOND}")
    break_it(tab, state)

    tab.create_document()

    assert expected in state["told"][-1][0]
    assert state["sent"] == [] and state["text"] == []
    assert service.history() == []


def test_отказ_в_подтверждении_ничего_не_отправляет_и_не_записывает(application, world):
    from app.core.marking import service

    tab, state = world
    state["yes"] = False
    tab.add_many(f"{FIRST}\n{SECOND}")

    tab.create_document()
    _settle(application)

    assert state["sent"] == [] and service.history() == []
    assert tab.codes == [FIRST, SECOND]
    # Вопрос назвал причину, число кодов и необратимость.
    text = "\n".join(state["text"])
    assert "Розничная продажа" in text and "Кодов: 2" in text and "нельзя" in text


def test_принятый_документ_выводит_коды_пишет_журнал_и_очищает_список(application, world):
    from app.core.marking import service

    tab, state = world
    state["answer"] = _done_status()
    tab.add_many(f"{FIRST}\n{SECOND}")
    changed: list = []
    tab.journal_changed.connect(lambda: changed.append(1))

    tab.create_document()
    _settle(application)

    document, thumbprint, group, contour = state["sent"][0]
    assert thumbprint == "ААББ" and group == "lp" and contour is Contour.SANDBOX
    assert document.codes == (FIRST, SECOND)
    [operation] = service.history()
    assert operation.kind is OperationKind.WITHDRAWAL
    assert operation.status is OperationStatus.DONE and operation.document_id == "док-1"
    assert operation.codes == [FIRST_KI, SECOND_KI]
    assert operation.reason == "RETAIL" and operation.product_group == "lp"
    assert changed                                    # журнал соседней вкладки обновится
    assert tab.codes == []                            # второй раз эти коды не уйдут
    assert "Выведено из оборота: 2" in state["told"][-1][0]


def test_отклонённый_документ_оставляет_коды_и_называет_причину(application, world):
    from app.core.marking import service

    tab, state = world
    state["answer"] = _done_status("CHECKED_NOT_OK", ["Код не в обороте"])
    tab.add_many(f"{FIRST}\n{SECOND}")

    tab.create_document()
    _settle(application)

    [operation] = service.history()
    assert operation.status is OperationStatus.REJECTED
    assert "Код не в обороте" in operation.error
    assert tab.codes == [FIRST, SECOND]
    assert "Вывод не выполнен" in state["told"][-1][0]


def test_документ_без_итога_не_объявляется_выполненным(application, world):
    from app.core.marking import service

    tab, state = world
    state["answer"] = _done_status("IN_PROGRESS")
    tab.add_many(f"{FIRST}\n{SECOND}")

    tab.create_document()
    _settle(application)

    [operation] = service.history()
    assert operation.status is OperationStatus.SENT and operation.document_id == "док-1"
    assert tab.codes == []                  # документ создан — повторно не отправлять
    assert "итога пока нет" in state["told"][-1][0]


def test_ошибка_до_запроса_оставляет_коды_и_помечает_операцию_неотправленной(
        application, world):
    from app.core.marking import service

    tab, state = world
    state["answer"] = introduce.IntroduceProblem("Подпись не удалась")
    tab.add_many(f"{FIRST}\n{SECOND}")

    tab.create_document()
    _settle(application)

    [operation] = service.history()
    assert operation.status is OperationStatus.FAILED and "Подпись" in operation.error
    assert tab.codes == [FIRST, SECOND]
    assert tab.send_button.isEnabled()      # после ошибки можно отправить заново


def test_неизвестный_исход_оставляет_коды_и_не_разрешает_повтора_вслепую(
        application, world, monkeypatch):
    from app.core.marking import service
    from app.ui.widgets import withdrawal_tab as module

    tab, state = world
    tab.add_many(f"{FIRST}\n{SECOND}")

    def unknown(document, thumbprint, group, contour, created):
        return "unknown", "Связь оборвалась, исход неизвестен"

    monkeypatch.setattr(module, "_send", unknown)
    tab.create_document()
    _settle(application)

    [operation] = service.history()
    assert operation.status is OperationStatus.SENT
    assert tab.codes == [FIRST, SECOND]
    assert "исход неизвестен" in state["told"][-1][0]


def test_такой_же_набор_кодов_называется_повтором_до_подтверждения(application, world):
    tab, state = world
    state["answer"] = _done_status()
    tab.add_many(f"{FIRST}\n{SECOND}")
    tab.create_document()
    _settle(application)
    state["text"].clear()

    tab.add_many(f"{SECOND}\n{FIRST}")        # те же коды, другой порядок
    tab.create_document()
    _settle(application)

    assert any("такой же набор кодов уже отправляли" in text for text in state["text"])


def test_обёртка_отправки_различает_ошибку_и_неизвестный_исход(monkeypatch):
    from app.ui.widgets import withdrawal_tab as module

    def boom(*args, **kwargs):
        raise introduce.IntroduceUnknown("оборвалось")

    monkeypatch.setattr(module.introduce, "send", boom)
    assert module._send(_document(), "ААББ", "lp", Contour.SANDBOX, lambda _id: None) \
        == ("unknown", "оборвалось")

    def refuse(*args, **kwargs):
        raise introduce.IntroduceProblem("не подписано")

    monkeypatch.setattr(module.introduce, "send", refuse)
    with pytest.raises(introduce.IntroduceProblem):
        module._send(_document(), "ААББ", "lp", Contour.SANDBOX, lambda _id: None)


def test_файл_сохраняется_без_обращения_к_системе(application, world, tmp_path,
                                                  monkeypatch):
    from app.ui.widgets import withdrawal_tab as module

    tab, state = world
    tab.add_many(f"{FIRST}\n{SECOND}")
    target = tmp_path / "вывод.json"
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))

    tab.save_file()

    assert target.read_text(encoding="utf-8") == tab.document().render()
    assert state["sent"] == [] and "Файл сохранён" in state["told"][-1][0]


def test_без_цены_вкладка_не_отправляет_и_говорит_почему(world):
    from app.ui.widgets.withdrawal_tab import PRICE_COLUMN

    tab, state = world
    tab.add_many(f"{FIRST}\n{SECOND}")
    tab.table.item(1, PRICE_COLUMN).setText("")

    tab.create_document()

    assert "цена за единицу у кодов: 1" in state["told"][-1][0]
    assert state["sent"] == [] and state["text"] == []


def test_общего_поля_цены_нет_цена_вписывается_в_строке(world):
    tab, _ = world

    assert not hasattr(tab, "price_edit")
    assert "Цена" in tab.table.horizontalHeaderItem(5).text()


def test_цена_строки_идёт_в_документ_и_повторяется_у_того_же_товара(world):
    from app.ui.widgets.withdrawal_tab import PRICE_COLUMN

    tab, _ = world
    tab.add_many(f"{FIRST}\n{SECOND}")
    tab.table.item(1, PRICE_COLUMN).setText("")

    tab.table.item(0, PRICE_COLUMN).setText("120")

    # Пустая цена у того же товара заполняется вписанной, а непустая остаётся.
    assert tab.table.item(1, PRICE_COLUMN).text() == "120"
    assert tab.document().code_prices == ("120", "120")
    assert [item["product_cost"] for item in json.loads(
        tab.document().render())["products"]] == [12000, 12000]


def test_колонка_цены_видна_только_для_причин_с_ценой(world):
    from app.ui.widgets.withdrawal_tab import PRICE_COLUMN

    tab, _ = world
    assert not tab.table.isColumnHidden(PRICE_COLUMN)

    tab.cause_box.setCurrentIndex(tab.cause_box.findData("LOSS"))

    assert tab.table.isColumnHidden(PRICE_COLUMN)


def test_строка_без_цены_подсвечена_а_с_ценой_нет(world):
    from app.ui.widgets.withdrawal_tab import PRICE_COLUMN

    tab, _ = world
    tab.add_code(FIRST)
    item = tab.table.item(0, PRICE_COLUMN)
    item.setText("")
    empty = item.background().color().name()

    item.setText("500")

    assert item.background().color().name() != empty


def test_ячейка_цены_редактируется_читаемо(world):
    from PySide6.QtWidgets import QLineEdit, QStyleOptionViewItem

    tab, _ = world
    tab.add_code(FIRST)
    delegate = tab.table.itemDelegate()

    editor = delegate.createEditor(tab.table, QStyleOptionViewItem(),
                                   tab.table.model().index(0, 5))

    assert isinstance(editor, QLineEdit) and "padding: 0 7px" in editor.styleSheet()


def test_наименование_стоит_после_кода_и_берётся_из_найденного(world):
    from app.ui.widgets import withdrawal_tab as module

    tab, _ = world
    assert [tab.table.horizontalHeaderItem(column).text()
            for column in (3, 4)] == ["Код", "Наименование"]
    tab._names_found({GTIN: "Футболка женская"})
    tab.add_code(FIRST)
    tab.add_code(SECOND)

    assert tab.table.item(0, module.NAME_COLUMN).text() == "Футболка женская"
    assert tab.table.item(1, module.NAME_COLUMN).text() == "Футболка женская"


def test_найденное_наименование_ложится_в_уже_добавленные_строки(world):
    from app.ui.widgets import withdrawal_tab as module

    tab, _ = world
    tab.add_many(f"{FIRST}\n{SECOND}")
    assert tab.table.item(0, module.NAME_COLUMN).text() == ""

    tab._names_found({GTIN: "Платье"})

    assert [tab.table.item(row, module.NAME_COLUMN).text() for row in range(2)] == [
        "Платье", "Платье"]


def test_наименование_можно_вписать_и_оно_идёт_всем_кодам_товара(world):
    from app.ui.widgets import withdrawal_tab as module

    tab, _ = world
    tab.add_many(f"{FIRST}\n{SECOND}")

    tab.table.item(0, module.NAME_COLUMN).setText("Джинсы")

    assert tab.table.item(1, module.NAME_COLUMN).text() == "Джинсы"


def test_наименование_ищется_в_кэше_и_у_системы_только_при_входе(monkeypatch, profile):
    from app.core.marking import service
    from app.core.marking.models import CodeInfo
    from app.ui.widgets import withdrawal_tab as module

    asked = []

    def check(codes, *, contour):
        asked.append(list(codes))
        return [CodeInfo(code=codes[0], gtin=GTIN, product_name="Куртка")]

    monkeypatch.setattr(service, "check", check)

    assert module._look_up_names([FIRST], Contour.SANDBOX, online=False) == {}
    assert asked == []
    assert module._look_up_names([FIRST], Contour.SANDBOX, online=True) == {
        GTIN: "Куртка"}
    assert len(asked) == 1


def test_инн_берётся_из_организации_входа_и_меняется_вместе_с_ней(
        application, tmp_path):
    from app.core.marking.session import Organisation
    from app.core.settings import AppSettings
    from app.ui.widgets.withdrawal_tab import WithdrawalTab

    current = {"inn": "250101802801"}
    known = [Organisation("7701234567", "ООО Ромашка"),
             Organisation("250101802801", "ИП Саух")]
    tab = WithdrawalTab(AppSettings(_path=tmp_path / "settings.json"),
                        inn=lambda: current["inn"], organisations=lambda: known)

    tab.prefill_inn()
    assert tab.inn_edit.text() == "250101802801"
    assert [tab.inn_box.itemData(i) for i in range(tab.inn_box.count())] == [
        "7701234567", "250101802801"]

    # Вошли под другой организацией: подставленный нами ИНН заменяется.
    current["inn"] = "7701234567"
    tab.prefill_inn()
    assert tab.inn_edit.text() == "7701234567"

    # Выбор в списке кладёт в поле ИНН, а не подпись с названием.
    tab.inn_box.activated.emit(1)
    assert tab.inn_edit.text() == "250101802801"

    # Вписанное руками повторный вход не затирает.
    tab.inn_edit.setText("7730306682")
    current["inn"] = "250101802801"
    tab.prefill_inn()
    assert tab.inn_edit.text() == "7730306682"


def test_вошедшая_организация_есть_в_списке_даже_если_её_не_запомнили(
        application, tmp_path):
    from app.core.settings import AppSettings
    from app.ui.widgets.withdrawal_tab import WithdrawalTab

    tab = WithdrawalTab(AppSettings(_path=tmp_path / "settings.json"),
                        inn=lambda: "250101802801")

    tab.prefill_inn()

    assert tab.inn_box.count() == 1 and tab.inn_box.itemData(0) == "250101802801"


def test_адрес_места_деятельности_запоминается_по_инн_после_отправки(
        application, world):
    tab, state = world
    tab.add_many(f"{FIRST}" + chr(10) + f"{SECOND}")
    tab.kpp_edit.setText("")
    state["answer"] = _done_status()

    tab.create_document()
    _settle(application)

    assert tab.settings.marking_places == {
        "250101802801": {"fias_id": FIAS, "kpp": ""}}


def test_отклонённый_документ_адрес_не_запоминает(application, world):
    tab, state = world
    tab.add_many(f"{FIRST}" + chr(10) + f"{SECOND}")
    state["answer"] = _done_status("CHECKED_NOT_OK", ["МОД не найдены"])

    tab.create_document()
    _settle(application)

    assert tab.settings.marking_places == {}


def test_запомненный_адрес_подставляется_для_этого_инн_и_не_чужой(
        application, tmp_path):
    from app.core.settings import AppSettings
    from app.ui.widgets.withdrawal_tab import WithdrawalTab

    settings = AppSettings(_path=tmp_path / "settings.json")
    settings.marking_places = {"2540098957": {"fias_id": FIAS, "kpp": "254001001"}}
    tab = WithdrawalTab(settings, inn=lambda: "2540098957")

    tab.prefill_inn()

    assert tab.fias_edit.text() == FIAS and tab.kpp_edit.text() == "254001001"

    tab.fias_edit.clear()
    tab.kpp_edit.clear()
    tab.inn_edit.setText("250101802801")
    tab.recall_place()

    assert tab.fias_edit.text() == "" and tab.kpp_edit.text() == ""


def test_без_фиас_вкладка_не_отправляет_и_говорит_почему(world):
    tab, state = world
    tab.add_many(f"{FIRST}" + chr(10) + f"{SECOND}")
    tab.fias_edit.clear()

    tab.create_document()

    assert "ФИАС" in state["told"][-1][0]
    assert state["sent"] == [] and state["text"] == []


def test_вкладка_стоит_в_маркировке_и_берёт_реквизиты_страницы(application, tmp_path):
    from app.core.settings import AppSettings
    from app.ui.marking_page import WITHDRAWAL_TAB, MarkingPage

    page = MarkingPage(AppSettings(_path=tmp_path / "settings.json"), lambda *_: None)

    assert page.tabs.tabText(WITHDRAWAL_TAB) == "Вывод из оборота"
    assert page.tabs.widget(WITHDRAWAL_TAB) is page.withdrawal_tab
    page.tabs.setCurrentIndex(WITHDRAWAL_TAB)
    assert re.fullmatch(r"\d*", page.withdrawal_tab.inn_edit.text())
