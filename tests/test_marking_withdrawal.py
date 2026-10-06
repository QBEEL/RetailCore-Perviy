"""Вывод из оборота: документ `LK_RECEIPT_XML` и вкладка со сканером.

Состав документа снят с ПРИНТМАРКИ (`marklib.dll`), и тесты держат его как есть:
порядок тегов, CDATA вокруг кода, причина и первичный документ. Сеть и КриптоПро
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


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    transport.limiter.reset()
    return tmp_path


def _document(**changes) -> Withdrawal:
    values = dict(inn="250101802801", cause="RETAIL", date="2026-10-06",
                  codes=(FIRST, SECOND), doc_type="RECEIPT", doc_number="145",
                  doc_date="2026-10-06")
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
    xml = _document().render()

    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<withdrawal version="8">\n'
        "  <trade_participant_inn>250101802801</trade_participant_inn>\n"
        "  <withdrawal_type>RETAIL</withdrawal_type>\n"
        "  <withdrawal_date>2026-10-06</withdrawal_date>\n"
        "  <primary_document_type>RECEIPT</primary_document_type>\n"
        "  <primary_document_number>145</primary_document_number>\n"
        "  <primary_document_date>2026-10-06</primary_document_date>\n"
        "  <products_list>\n"
        "    <product>\n"
        f"      <cis><![CDATA[{FIRST_KI}]]></cis>\n"
        "    </product>\n"
        "    <product>\n"
        f"      <cis><![CDATA[{SECOND_KI}]]></cis>\n"
        "    </product>\n"
        "  </products_list>\n"
        "</withdrawal>\n")


def test_в_документ_идёт_код_идентификации_без_криптохвоста():
    xml = _document().render()

    assert "91EE11" not in xml and "92dGVz" not in xml and GS not in xml


def test_причина_другое_требует_описания_и_пишется_в_свой_тег():
    assert any("«Другое»" in problem
               for problem in _document(cause="OTHER").problems)

    xml = _document(cause="OTHER", cause_other="Подарок сотруднику").render()

    assert "<withdrawal_type>OTHER</withdrawal_type>" in xml
    assert "<withdrawal_type_other>Подарок сотруднику</withdrawal_type_other>" in xml
    # Описание причины стоит между причиной и датой, как в шаблоне.
    assert xml.index("withdrawal_type_other") < xml.index("<withdrawal_date>")


def test_описание_чужой_причины_в_документ_не_попадает():
    xml = _document(cause_other="лишнее").render()

    assert "withdrawal_type_other" not in xml and "лишнее" not in xml


def test_иной_первичный_документ_требует_наименования():
    other = _document(doc_type="OTHER", doc_number="7", doc_date="2026-10-05")
    assert any("наименование" in problem for problem in other.problems)

    xml = _document(doc_type="OTHER", doc_number="7", doc_date="2026-10-05",
                    doc_name="Акт списания").render()

    assert "<primary_document_custom_name>Акт списания</primary_document_custom_name>" in xml


def test_первичный_документ_заполняется_целиком_или_не_заполняется():
    assert any("целиком" in problem for problem in _document(doc_number="").problems)

    xml = _document(doc_type="", doc_number="", doc_date="").render()

    assert "primary_document" not in xml


def test_спецсимволы_экранируются_а_конец_cdata_не_рвёт_разметку():
    xml = _document(cause="OTHER", cause_other='Брак <"А"> & Ко').render()

    assert "Брак &lt;\"А\"&gt; &amp; Ко" in xml


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
    xml = _document(cause="STATE_CONTRACT", state_contract_id="К-77").render()

    assert "<state_contract_id>К-77</state_contract_id>" in xml
    assert "state_contract_id" not in _document().render()


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


def test_вывод_уходит_единым_методом_типом_lk_receipt_xml(served):
    calls, answers, signed = served
    answers.append("11111111-2222-3333-4444-555555555555")

    doc_id = introduce.submit(_document(), "ААББ", "lp", Contour.SANDBOX)

    assert doc_id == "11111111-2222-3333-4444-555555555555"
    request = calls[0]
    assert "/lk/documents/create" in request.full_url and "pg=lp" in request.full_url
    body = json.loads(request.data)
    assert set(body) == {"document_format", "product_document", "type", "signature"}
    assert body["document_format"] == "XML" and body["type"] == "LK_RECEIPT_XML"
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
    target = tmp_path / "вывод.xml"
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))

    tab.save_file()

    assert target.read_text(encoding="utf-8") == tab.document().render()
    assert state["sent"] == [] and "Файл сохранён" in state["told"][-1][0]


def test_вкладка_стоит_в_маркировке_и_берёт_реквизиты_страницы(application, tmp_path):
    from app.core.settings import AppSettings
    from app.ui.marking_page import WITHDRAWAL_TAB, MarkingPage

    page = MarkingPage(AppSettings(_path=tmp_path / "settings.json"), lambda *_: None)

    assert page.tabs.tabText(WITHDRAWAL_TAB) == "Вывод из оборота"
    assert page.tabs.widget(WITHDRAWAL_TAB) is page.withdrawal_tab
    page.tabs.setCurrentIndex(WITHDRAWAL_TAB)
    assert re.fullmatch(r"\d*", page.withdrawal_tab.inn_edit.text())
