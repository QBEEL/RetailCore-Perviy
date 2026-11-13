"""Формирование наборов из ОСУ: документ `AGGREGATION_DOCUMENT` (JSON) и вкладка `SetsTab`.

Проверяются:
- точная схема True API `AGGREGATION_DOCUMENT` (`participantId`, `aggregationUnits`,
  `unitSerialNumberType: "PRODUCT_SET"`, `aggregationType: "AGGREGATION"`,
  `sntins` без криптохвоста); вместимость проверяется локально;
- правила «Честного ЗНАКа» (редакция 19.10.2026) для товарной группы
  «Косметика и бытовая химия» (`cosmetics`) при объёмно-сортовом учёте (ОСУ);
- проверка форматов, дублей, статусов КИН и вложений, предпросмотр состава;
- атомарное сохранение и восстановление черновика набора и номера документа;
- отправка через `/lk/documents/create?pg=cosmetics` без повторной отправки при
  обрыве связи и строгий опрос статуса по `doc_id`.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.marking import introduce, service, sets, store, transport
from app.core.marking.codes import GS
from app.core.marking.models import (
    CodeInfo,
    CodeState,
    Contour,
    Operation,
    OperationKind,
    OperationStatus,
)
from app.core.marking.sets import SetDocument, SetDraft, SetUnit

SET_GTIN = "04607001239994"
ITEM1_GTIN = "04607001230014"
ITEM2_GTIN = "04607001230021"

SET_RAW = f"01{SET_GTIN}21SET001{GS}91EE11{GS}92dGVzdHNldA=="
SET_KI = f"01{SET_GTIN}21SET001"

ITEM1_RAW = f"01{ITEM1_GTIN}21ITM001{GS}91EE11{GS}92dGVzdGl0bTE="
ITEM1_KI = f"01{ITEM1_GTIN}21ITM001"

ITEM2_RAW = f"01{ITEM2_GTIN}21ITM002{GS}91EE11{GS}92dGVzdGl0bTI="
ITEM2_KI = f"01{ITEM2_GTIN}21ITM002"

OUR_INN = "7701234567"
SUPPLIER_INN = "7899887766"


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    transport.limiter.reset()
    return tmp_path


def _doc(**changes) -> SetDocument:
    values = dict(
        inn=OUR_INN,
        set_code=SET_RAW,
        item_codes=(ITEM1_RAW, ITEM2_RAW),
        product_group="cosmetics",
        capacity=None,
    )
    values.update(changes)
    return SetDocument.single(**values)


# --- схема JSON AGGREGATION_DOCUMENT -----------------------------------------------

def test_документ_агрегации_набора_собирается_по_схеме_true_api():
    doc = _doc()
    text = doc.render()
    body = json.loads(text)

    assert doc.wire_format == "MANUAL"
    assert doc.wire_type("cosmetics") == "AGGREGATION_DOCUMENT"
    assert body == {
        "participantId": OUR_INN,
        "aggregationUnits": [
            {
                "aggregationType": "AGGREGATION",
                "sntins": [ITEM1_KI, ITEM2_KI],
                "unitSerialNumber": SET_KI,
                "unitSerialNumberType": "PRODUCT_SET",
            }
        ],
    }
    # Криптохвост и разделители GS1 в документ не попадают.
    assert "91EE11" not in text and "92dGVz" not in text and GS not in text


def test_вместимость_набора_остаётся_локальной_проверкой():
    doc = _doc(capacity=5)
    unit_json = json.loads(doc.render())["aggregationUnits"][0]

    assert "aggregatedItemsCount" not in unit_json
    assert "aggregationUnitCapacity" not in unit_json
    assert doc.units[0].effective_capacity == 5


def test_некорректные_реквизиты_и_коды_блокируют_сборку():
    assert any("ИНН" in p for p in _doc(inn="123").problems)
    assert any("товарная группа" in p for p in _doc(product_group="").problems)
    assert any("КИН" in p for p in _doc(set_code="").problems)
    assert any("КИН" in p for p in _doc(set_code="МУСОР").problems)
    assert any("нет ни одного кода вложения" in p for p in _doc(item_codes=()).problems)
    assert any("ошибкой формата" in p for p in _doc(item_codes=(ITEM1_RAW, "МУСОР")).problems)
    assert any(
        "вместимость набора" in p
        for p in _doc(capacity=1, item_codes=(ITEM1_RAW, ITEM2_RAW)).problems
    )
    with pytest.raises(introduce.IntroduceProblem):
        _doc(set_code="").render()


def test_повторы_вложений_и_совпадение_с_кин_отклоняются():
    same_item_other_tail = f"01{ITEM1_GTIN}21ITM001{GS}91EE11{GS}92другой=="
    assert any(
        "повторяющиеся коды" in p
        for p in _doc(item_codes=(ITEM1_RAW, same_item_other_tail)).problems
    )
    assert any(
        "совпадает с одним из кодов вложений" in p
        for p in _doc(item_codes=(ITEM1_RAW, SET_RAW)).problems
    )


# --- предпросмотр состава набора ---------------------------------------------------

def test_предпросмотр_состава_группирует_вложения_по_gtin_и_подставляет_названия():
    item1_b = f"01{ITEM1_GTIN}21ITM003{GS}91EE11{GS}92dGVzdA=="
    unit = SetUnit(set_code=SET_RAW, item_codes=(ITEM1_RAW, item1_b, ITEM2_RAW))
    checks = {
        SET_KI: CodeInfo(code=SET_KI, gtin=SET_GTIN, product_name="Подарочный набор"),
        ITEM1_KI: CodeInfo(code=ITEM1_KI, gtin=ITEM1_GTIN, product_name="Шампунь 250 мл"),
    }
    names = {ITEM2_GTIN: "Бальзам 250 мл"}

    comp = sets.composition(unit, checks=checks, names=names)

    assert comp.set_ki == SET_KI
    assert comp.set_gtin == SET_GTIN
    assert comp.set_name == "Подарочный набор"
    assert comp.total_items == 3
    assert comp.distinct_gtins == 2
    assert [(i.gtin, i.name, i.quantity) for i in comp.items] == [
        (ITEM1_GTIN, "Шампунь 250 мл", 2),
        (ITEM2_GTIN, "Бальзам 250 мл", 1),
    ]


# --- правила проверки статусов КИН и вложений (ОСУ, косметика) ---------------------

def _checked_set(
    *,
    state: CodeState = CodeState.APPLIED,
    owner_inn: str = OUR_INN,
    package_type: str = "SET",
    emission_type: str = "LOCAL",
    product_group: str = "cosmetics",
    children: tuple[str, ...] = (),
) -> CodeInfo:
    return CodeInfo(
        code=SET_KI,
        gtin=SET_GTIN,
        serial="SET001",
        state=state,
        valid=True,
        found=True,
        owner_inn=owner_inn,
        product_name="Набор косметический",
        product_group=product_group,
        package_type=package_type,
        emission_type=emission_type,
        children=children,
        permits=[],
        permit_exempt="не требуется",
    )


def _checked_item(
    ki: str,
    gtin: str,
    *,
    state: CodeState = CodeState.INTRODUCED,
    owner_inn: str = SUPPLIER_INN,
    owner_name: str = "ООО Поставщик ОСУ",
    package_type: str = "UNIT",
    emission_type: str = "FOREIGN",
    product_group: str = "cosmetics",
) -> CodeInfo:
    return CodeInfo(
        code=ki,
        gtin=gtin,
        serial=ki[-6:],
        state=state,
        valid=True,
        found=True,
        owner_inn=owner_inn,
        owner_name=owner_name,
        product_name="Средство косметическое",
        product_group=product_group,
        package_type=package_type,
        emission_type=emission_type,
        permits=[],
        permit_exempt="не требуется",
    )


def test_осу_косметика_вложения_в_обороте_от_поставщика_и_кин_нанесён_проходят():
    """Типовой сценарий ОСУ для косметики и бытовой химии:
    - вложения приняты по ОСУ (числятся за поставщиком, статус «В обороте», любой способ выпуска);
    - КИН заказан нами со способом выпуска «Произведён в РФ» (`LOCAL`) и находится в статусе «Нанесён»;
    - проверка проходит без ошибок и поясняет переход права собственности и авто-ввод набора в оборот.
    """
    unit = SetUnit(set_code=SET_RAW, item_codes=(ITEM1_RAW, ITEM2_RAW))
    checks = {
        SET_KI: _checked_set(state=CodeState.APPLIED, emission_type="LOCAL"),
        ITEM1_KI: _checked_item(ITEM1_KI, ITEM1_GTIN, owner_inn=SUPPLIER_INN),
        ITEM2_KI: _checked_item(ITEM2_KI, ITEM2_GTIN, owner_inn=SUPPLIER_INN),
    }

    verdict = sets.verify_codes(
        unit, checks, our_inn=OUR_INN, product_group="cosmetics"
    )

    assert verdict.ok
    assert verdict.problems == ()
    assert any("автоматически перейдёт в статус «В обороте»" in n for n in verdict.notes)
    assert any("ОСУ" in n and "перейдут в собственность" in n for n in verdict.notes)


def test_для_группы_без_осу_чужие_вложения_блокируются():
    unit = SetUnit(set_code=SET_RAW, item_codes=(ITEM1_RAW,))
    checks = {
        SET_KI: _checked_set(product_group="shoes"),
        ITEM1_KI: _checked_item(
            ITEM1_KI, ITEM1_GTIN, owner_inn=SUPPLIER_INN, product_group="shoes"
        ),
    }

    verdict = sets.verify_codes(unit, checks, our_inn=OUR_INN, product_group="shoes")

    assert not verdict.ok
    assert any("принадлежит другому участнику" in p for p in verdict.problems)


def test_чужой_кин_или_неверный_статус_кин_блокируются():
    unit = SetUnit(set_code=SET_RAW, item_codes=(ITEM1_RAW,))
    base_item = {ITEM1_KI: _checked_item(ITEM1_KI, ITEM1_GTIN)}

    # КИН принадлежит другому участнику
    v_owner = sets.verify_codes(
        unit,
        {SET_KI: _checked_set(owner_inn=SUPPLIER_INN), **base_item},
        our_inn=OUR_INN,
    )
    assert any("принадлежит другому участнику" in p for p in v_owner.problems)

    # КИН в статусе EMlTTED (не подан отчёт о нанесении)
    v_emitted = sets.verify_codes(
        unit,
        {SET_KI: _checked_set(state=CodeState.EMITTED), **base_item},
        our_inn=OUR_INN,
    )
    assert any("отчёт о нанесении" in p for p in v_emitted.problems)

    # КИН уже в обороте
    v_intro = sets.verify_codes(
        unit,
        {SET_KI: _checked_set(state=CodeState.INTRODUCED), **base_item},
        our_inn=OUR_INN,
    )
    assert any("уже в статусе «В обороте»" in p for p in v_intro.problems)

    # Вместо КИН отсканирована обычная единица товара (UNIT)
    v_unit = sets.verify_codes(
        unit,
        {SET_KI: _checked_set(package_type="UNIT"), **base_item},
        our_inn=OUR_INN,
    )
    assert any("ожидался SET" in p for p in v_unit.problems)


def test_неизвестные_поля_и_вложенные_коды_не_проходят_проверку():
    unit = SetUnit(set_code=SET_RAW, item_codes=(ITEM1_RAW,))
    kin = _checked_set(emission_type="", package_type="")
    item = _checked_item(ITEM1_KI, ITEM1_GTIN, package_type="SET")
    item.raw = {"cisInfo": {"statusEx": "BLOCKED", "parent": "другой-набор"}}

    verdict = sets.verify_codes(
        unit, {SET_KI: kin, ITEM1_KI: item}, our_inn=OUR_INN
    )

    assert not verdict.ok
    assert any("тип упаковки кода набора" in p for p in verdict.problems)
    assert any("способ выпуска КИН" in p for p in verdict.problems)
    assert any("неподходящий тип упаковки" in p for p in verdict.problems)
    assert any("особое состояние" in p for p in verdict.problems)
    assert any("уже находится в упаковке" in p for p in verdict.problems)


def test_разные_статусы_вложений_и_неверный_способ_выпуска_кин_блокируются():
    unit = SetUnit(set_code=SET_RAW, item_codes=(ITEM1_RAW, ITEM2_RAW))

    # Одно вложение «В обороте», другое «Нанесён»
    v_mixed = sets.verify_codes(
        unit,
        {
            SET_KI: _checked_set(state=CodeState.APPLIED, emission_type="LOCAL"),
            ITEM1_KI: _checked_item(ITEM1_KI, ITEM1_GTIN, state=CodeState.INTRODUCED),
            ITEM2_KI: _checked_item(ITEM2_KI, ITEM2_GTIN, state=CodeState.APPLIED),
        },
        our_inn=OUR_INN,
    )
    assert not v_mixed.ok
    assert any("разные статусы" in p for p in v_mixed.problems)

    # Вложения «В обороте», но КИН выпущен как «Маркировка остатков» (REMAINS)
    v_remains = sets.verify_codes(
        unit,
        {
            SET_KI: _checked_set(state=CodeState.APPLIED, emission_type="REMAINS"),
            ITEM1_KI: _checked_item(ITEM1_KI, ITEM1_GTIN, state=CodeState.INTRODUCED),
            ITEM2_KI: _checked_item(ITEM2_KI, ITEM2_GTIN, state=CodeState.INTRODUCED),
        },
        our_inn=OUR_INN,
    )
    assert not v_remains.ok
    assert any("Произведён в РФ" in p for p in v_remains.problems)


# --- разбор `/cises/info`, кэш и опрос операций ------------------------------------

def test_разбор_cises_info_сохраняет_группу_тип_упаковки_и_способ_выпуска(tmp_path):
    db = str(tmp_path / "marking.db")
    rows = [
        {
            "cisInfo": {
                "requestedCis": SET_KI,
                "cis": SET_KI,
                "gtin": SET_GTIN,
                "status": "APPLIED",
                "productGroup": "cosmetics",
                "packageType": "SET",
                "emissionType": "LOCAL",
                "child": [ITEM1_KI, ITEM2_KI],
                "certDoc": [],
            }
        }
    ]
    parsed = service._parse_check(rows, [SET_KI])
    assert len(parsed) == 1
    info = parsed[0]
    assert info.product_group == "cosmetics"
    assert info.package_type == "SET"
    assert info.emission_type == "LOCAL"
    assert info.children == (ITEM1_KI, ITEM2_KI)

    store.remember_checks([info], Contour.SANDBOX, db)
    cached = store.known_checks([SET_KI], Contour.SANDBOX, db)[SET_KI]
    assert cached.product_group == "cosmetics"
    assert cached.package_type == "SET"
    assert cached.emission_type == "LOCAL"
    assert cached.children == (ITEM1_KI, ITEM2_KI)


def test_операция_формирования_набора_опрашивается_по_номеру_документа(tmp_path):
    db = str(tmp_path / "marking.db")
    op = service.start(
        OperationKind.AGGREGATION,
        [SET_KI, ITEM1_KI, ITEM2_KI],
        group="cosmetics",
        contour=Contour.SANDBOX,
        db_path=db,
    )
    op.status = OperationStatus.SENT
    op.document_id = "doc-set-1"
    store.update(op, db)

    assert not OperationKind.AGGREGATION.reversible
    assert service.can_refresh(op)


# --- черновик набора ---------------------------------------------------------------

def test_черновик_набора_атомарно_сохраняется_и_восстанавливается(tmp_path):
    path = str(tmp_path / "draft.json")
    draft = SetDraft(
        inn=OUR_INN,
        product_group="cosmetics",
        set_code=SET_RAW,
        capacity=2,
        item_codes=[ITEM1_RAW, ITEM2_RAW],
        names={ITEM1_GTIN: "Гель для душа"},
        operation_id=7,
        doc_id="doc-77",
        doc_status="Обрабатывается",
    )
    sets.save_draft(draft, path)

    loaded = sets.load_draft(path)
    assert loaded.inn == OUR_INN
    assert loaded.product_group == "cosmetics"
    assert loaded.set_code == SET_RAW
    assert loaded.capacity == 2
    assert loaded.item_codes == [ITEM1_RAW, ITEM2_RAW]
    assert loaded.names == {ITEM1_GTIN: "Гель для душа"}
    assert loaded.operation_id == 7
    assert loaded.doc_id == "doc-77"
    assert loaded.has_pending_doc

    sets.clear_draft(path)
    assert not os.path.exists(path)
    assert sets.load_draft(path).set_code == ""


# --- отправка в True API -----------------------------------------------------------

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
        introduce.crypto,
        "sign",
        lambda data, thumbprint, detached=False: signed.append((data, detached)) or "ПОДПИСЬ",
    )
    return calls, answers, signed


def test_формирование_набора_уходит_типом_aggregation_document_json(served):
    calls, answers, signed = served
    answers.append("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    doc = _doc()

    doc_id = introduce.submit(doc, "ААББ", "cosmetics", Contour.SANDBOX)

    assert doc_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    request = calls[0]
    assert "/lk/documents/create" in request.full_url
    assert "pg=cosmetics" in request.full_url
    body = json.loads(request.data)
    assert body["document_format"] == "MANUAL"
    assert body["type"] == "AGGREGATION_DOCUMENT"
    sent_json = base64.b64decode(body["product_document"]).decode("utf-8")
    assert signed == [(sent_json, True)]
    assert sent_json == doc.render()


# --- интерфейс вкладки SetsTab -----------------------------------------------------

@pytest.fixture(scope="module")
def application():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def world(application, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from app.core.settings import AppSettings
    from app.ui.widgets import sets_tab as module

    state = {
        "signed": True,
        "yes": True,
        "text": [],
        "sent": [],
        "answer": None,
        "thumbprint": "ААББ",
        "contour": Contour.SANDBOX,
        "told": [],
        "checked": [],
        "polled": None,
    }

    class Session:
        contour = Contour.SANDBOX
        inn = OUR_INN

    class FakeBox:
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
            if role == QMessageBox.ButtonRole.AcceptRole:
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
        created("док-набор-1")
        return "sent", answer

    monkeypatch.setattr(module, "QMessageBox", FakeBox)
    monkeypatch.setattr(module, "_send", send)
    monkeypatch.setattr(module, "_look_up_names", lambda *_: {})
    monkeypatch.setattr(
        module,
        "_check_codes",
        lambda codes, contour: state["checked"],
    )
    monkeypatch.setattr(service, "signed_in", lambda: state["signed"])
    monkeypatch.setattr(service, "current", lambda: Session)

    draft_file = str(tmp_path / "sets_draft.json")
    tab = module.SetsTab(
        AppSettings(_path=tmp_path / "settings.json"),
        notify=lambda text, kind: state["told"].append((text, kind)),
        contour=lambda: state["contour"],
        thumbprint=lambda: state["thumbprint"],
        inn=lambda: OUR_INN,
        draft_path=draft_file,
    )
    tab.prefill_inn()
    return tab, state, draft_file


def _settle(application) -> None:
    from PySide6.QtCore import QThreadPool

    for _ in range(6):
        QThreadPool.globalInstance().waitForDone(3000)
        application.processEvents()


def test_сканирование_кин_и_вложений_заполняет_таблицу_и_сводку(world):
    tab, _, _ = world

    assert tab.group == "cosmetics"
    assert tab.set_set_code(SET_RAW) == "added"
    assert tab.add_code(ITEM1_RAW) == "added"
    assert tab.add_code(ITEM2_RAW) == "added"

    assert tab.set_code == SET_RAW
    assert tab.codes == [ITEM1_RAW, ITEM2_RAW]
    assert tab.table.rowCount() == 2
    assert tab.tile_items._value.text() == "2"
    assert tab.tile_gtins._value.text() == "2"
    assert ITEM1_GTIN in tab.composition_label.text()
    assert ITEM2_GTIN in tab.composition_label.text()


def test_сканирование_кин_в_поле_вложений_или_дубля_отклоняется(world):
    tab, _, _ = world
    tab.set_set_code(SET_RAW)
    tab.add_code(ITEM1_RAW)

    assert tab.add_code(SET_RAW) == "repeat"
    assert tab.add_code(ITEM1_RAW) == "repeat"
    assert tab.add_code("НЕ_КОД") == "broken"
    assert tab.codes == [ITEM1_RAW]
    assert tab.tile_repeats._value.text() == "2"
    assert tab.tile_broken._value.text() == "1"


def test_проверка_кодов_в_чз_отображает_статусы_и_блокирует_отправку_при_ошибке(
    application, world
):
    tab, state, _ = world
    tab.set_set_code(SET_RAW)
    tab.add_code(ITEM1_RAW)
    # КИН в статусе EMITTED — ошибка (нужен отчёт о нанесении)
    state["checked"] = [
        _checked_set(state=CodeState.EMITTED),
        _checked_item(ITEM1_KI, ITEM1_GTIN, state=CodeState.INTRODUCED),
    ]

    tab.check_codes_in_gis()
    _settle(application)

    assert not tab._verification.ok
    assert "отчёт о нанесении" in tab.hint.text()
    tab.create_document()
    assert state["sent"] == []
    assert "отчёт о нанесении" in state["told"][-1][0]


def test_успешное_формирование_набора_записывает_операцию_и_очищает_черновик(
    application, world
):
    tab, state, draft_file = world
    tab.set_set_code(SET_RAW)
    tab.add_many(f"{ITEM1_RAW}\n{ITEM2_RAW}")
    state["answer"] = introduce.Sent(
        "док-набор-1", introduce.DocStatus(code="CHECKED_OK")
    )

    tab.create_document()
    _settle(application)

    assert len(state["sent"]) == 1
    sent_doc, thumbprint, group, contour = state["sent"][0]
    assert group == "cosmetics" and thumbprint == "ААББ" and contour is Contour.SANDBOX
    assert json.loads(sent_doc.render())["aggregationUnits"][0]["unitSerialNumber"] == SET_KI

    [op] = service.history()
    assert op.kind is OperationKind.AGGREGATION
    assert op.status is OperationStatus.DONE
    assert op.document_id == "док-набор-1"
    assert op.codes == [SET_KI, ITEM1_KI, ITEM2_KI]
    assert tab.codes == [] and tab.set_code == ""
    assert not os.path.exists(draft_file)


def test_незавершённый_документ_сохраняется_в_черновике_и_опрашивается_без_повтора(
    application, world, tmp_path, monkeypatch
):
    from app.core.settings import AppSettings
    from app.ui.widgets import sets_tab as module

    tab, state, draft_file = world
    tab.set_set_code(SET_RAW)
    tab.add_code(ITEM1_RAW)
    state["answer"] = introduce.Sent(
        "док-набор-1", introduce.DocStatus(code="IN_PROGRESS")
    )

    tab.create_document()
    _settle(application)

    [op] = service.history()
    assert op.status is OperationStatus.SENT
    assert tab._doc_id == "док-набор-1"
    # Повторная отправка того же черновика заблокирована, пока документ в обработке.
    assert not tab.send_button.isEnabled()
    assert not tab.clear_button.isEnabled()
    assert tab.poll_button.isEnabled()
    tab.clear()
    assert tab.set_code == SET_RAW and tab.codes == [ITEM1_RAW]

    # Переоткрываем вкладку — черновик и номер документа восстанавливаются с диска.
    reopened = module.SetsTab(
        AppSettings(_path=tmp_path / "settings.json"),
        notify=lambda text, kind: state["told"].append((text, kind)),
        contour=lambda: state["contour"],
        thumbprint=lambda: state["thumbprint"],
        inn=lambda: OUR_INN,
        draft_path=draft_file,
    )
    assert reopened._doc_id == "док-набор-1"
    assert reopened.set_code == SET_RAW
    assert reopened.codes == [ITEM1_RAW]

    # Опрашиваем статус документа — он стал CHECKED_OK, повторной отправки нет.
    monkeypatch.setattr(
        module.introduce,
        "status",
        lambda doc_id, pg, contour: introduce.DocStatus(code="CHECKED_OK"),
    )
    reopened.poll_document_status()
    _settle(application)

    assert len(state["sent"]) == 1  # повторно документ не отправлялся
    [updated_op] = service.history()
    assert updated_op.status is OperationStatus.DONE
    assert reopened.codes == [] and reopened.set_code == ""


def test_сбой_после_создания_документа_сохраняет_id_и_запрещает_повтор(
    application, world, monkeypatch
):
    from app.ui.widgets import sets_tab as module

    tab, state, draft_file = world
    tab.set_set_code(SET_RAW)
    tab.add_code(ITEM1_RAW)

    def interrupted(document, thumbprint, group, contour, created):
        created("док-набор-сбой")
        raise RuntimeError("опрос прервался")

    monkeypatch.setattr(module, "_send", interrupted)
    tab.create_document()
    _settle(application)

    [op] = service.history()
    assert op.document_id == "док-набор-сбой"
    assert op.status is OperationStatus.SENT
    assert sets.load_draft(draft_file).doc_id == "док-набор-сбой"
    tab.create_document()
    assert "повторная отправка заблокирована" in state["told"][-1][0]


def test_окно_заказа_кодов_поддерживает_выбор_типа_упаковки_set_для_кин(application):
    from app.ui.widgets.order_dialog import OrderDialog

    dlg = OrderDialog(known_orders=[], has_certificate=True, contact="Иванов И.И.")
    dlg.group_box.setCurrentIndex(dlg.group_box.findData("cosmetics"))
    dlg.cis_type_box.setCurrentIndex(dlg.cis_type_box.findData("SET"))
    dlg.add_line(SET_GTIN, 10)

    req = dlg.request
    assert req.product_group == "cosmetics"
    assert len(req.lines) == 1
    assert req.lines[0].cis_type == "SET"
    assert req.lines[0].as_json()["cisType"] == "SET"

    dlg.group_box.setCurrentIndex(dlg.group_box.findData("shoes"))
    assert dlg.cis_type_box.currentData() == "UNIT"
    assert not dlg.cis_type_box.isEnabled()


def test_страница_маркировки_содержит_вкладку_формирования_наборов(
    application, tmp_path
):
    from app.core.settings import AppSettings
    from app.ui.marking_page import SETS_TAB, TAB_SUBTITLES, MarkingPage

    settings = AppSettings(_path=str(tmp_path / "settings.json"))
    page = MarkingPage(settings, lambda *_: None)

    assert page.tabs.tabText(SETS_TAB) == "Формирование наборов"
    page.tabs.setCurrentIndex(SETS_TAB)
    application.processEvents()
    assert page.subtitle.text() == TAB_SUBTITLES[SETS_TAB]


def test_группа_chemistry_от_чз_принимается_как_косметика_и_бытовая_химия(
    application, world
):
    tab, state, _ = world
    tab.set_set_code(SET_RAW)
    tab.add_code(ITEM1_RAW)
    tab.add_code(ITEM2_RAW)

    # В ответах `/cises/info` ЧЗ возвращает `productGroup: "chemistry"` для группы
    # «Косметика и бытовая химия», тогда как в UI выбрана `cosmetics`.
    state["checked"] = [
        _checked_set(product_group="chemistry", state=CodeState.APPLIED),
        _checked_item(
            ITEM1_KI,
            ITEM1_GTIN,
            product_group="chemistry",
            owner_inn=SUPPLIER_INN,
            state=CodeState.INTRODUCED,
        ),
        _checked_item(
            ITEM2_KI,
            ITEM2_GTIN,
            product_group="chemistry",
            owner_inn=SUPPLIER_INN,
            state=CodeState.INTRODUCED,
        ),
    ]

    tab.check_codes_in_gis()
    _settle(application)

    assert tab._verification is not None
    assert tab._verification.ok, tab._verification.problems
    assert tab.effective_group == "chemistry"

    state["answer"] = introduce.Sent(
        "док-набор-chem", introduce.DocStatus(code="CHECKED_OK")
    )
    tab.create_document()
    _settle(application)

    assert len(state["sent"]) == 1
    sent_doc, _thumb, group, _contour = state["sent"][0]
    assert group == "chemistry"
    assert sent_doc.wire_type(group) == "AGGREGATION_DOCUMENT"


def test_загрузка_csv_с_кодами_наборов_кин_и_переключение_пула(
    application, world, tmp_path
):
    tab, state, _ = world
    set2_ki = f"01{SET_GTIN}21SET0000000002"
    set2_raw = f"{set2_ki}\x1d91EE06\x1d92dGVzdC1zZXQtY3J5cHRvLTI="

    # CSV с заголовком, кавычками и несколькими колонками (выгрузка СУЗ / ЧЗ)
    csv_file = tmp_path / "set_codes.csv"
    csv_content = (
        "КИ;GTIN;Тип упаковки;Статус\n"
        f'"{SET_RAW}";{SET_GTIN};SET;APPLIED\n'
        f'"{set2_raw}";{SET_GTIN};SET;APPLIED\n'
    )
    csv_file.write_text(csv_content, encoding="utf-8-sig")

    loaded = tab.load_set_file(str(csv_file))
    assert loaded == 2
    assert tab.set_code == SET_RAW
    assert len(tab.set_codes_pool) == 2
    assert tab.batch_mode is True
    assert not tab.set_pool_widget.isHidden()
    assert tab.set_pool_box.count() == 3  # «Все КИН из CSV» + 2 одиночных КИН

    # Переключение на следующий одиночный КИН кнопкой «Следующий КИН»
    assert tab.next_set_code() is True
    assert tab.batch_mode is False
    assert tab.set_code == set2_raw
    assert tab.next_set_code() is True
    assert tab.set_code == SET_RAW

    # После успешного формирования первого набора автоматически выбирается оставшийся КИН
    tab.add_code(ITEM1_RAW)
    state["answer"] = introduce.Sent(
        "док-набор-пул-1", introduce.DocStatus(code="CHECKED_OK")
    )
    tab.create_document()
    _settle(application)

    assert tab.set_code == set2_raw
    assert tab.codes == []
    assert tab.set_codes_pool == []


def test_формирование_партии_из_40_наборов_из_csv_распределяет_вложения_по_наборам(
    application, world, tmp_path
):
    tab, state, _ = world

    # Синтетические 40 КИН наборов (включая серийные номера со спецсимволами `,`, `;`, `"`)
    special_serials = ["5I6GQQ", "5Xi,i:", "5Db;IZ", '5"rSK5', '5ewr0"']
    set_kis = [
        f"01{SET_GTIN}21" + (special_serials[i] if i < len(special_serials) else f"S{i:05d}")
        for i in range(40)
    ]
    item1_kis = [f"01{ITEM1_GTIN}21A{i:05d}" for i in range(40)]
    item2_kis = [f"01{ITEM2_GTIN}21B{i:05d}" for i in range(40)]

    nabor_csv = tmp_path / "nabor.csv"
    krem_csv = tmp_path / "krem.csv"
    karma_csv = tmp_path / "karma.csv"
    nabor_csv.write_text("\r\n".join(set_kis) + "\r\n", encoding="utf-8")
    krem_csv.write_text("\r\n".join(item1_kis) + "\r\n", encoding="utf-8")
    karma_csv.write_text("\r\n".join(item2_kis) + "\r\n", encoding="utf-8")

    assert tab.load_set_file(str(nabor_csv)) == 40
    assert tab.batch_mode is True
    assert len(tab.active_set_codes) == 40

    assert tab.load_file([str(krem_csv), str(karma_csv)]) == (80, 0, 0)
    assert len(tab.codes) == 80

    doc = tab.document()
    assert not doc.problems, doc.problems
    assert len(doc.units) == 40
    # В каждом из 40 наборов ровно 2 вложения (по 1 каждого GTIN), а не 80 в первом!
    for i, unit in enumerate(doc.units):
        assert unit.set_ki == set_kis[i]
        assert unit.item_kis == (item1_kis[i], item2_kis[i])

    rendered = json.loads(doc.render())
    assert len(rendered["aggregationUnits"]) == 40
    assert rendered["aggregationUnits"][0] == {
        "aggregationType": "AGGREGATION",
        "sntins": [item1_kis[0], item2_kis[0]],
        "unitSerialNumber": set_kis[0],
        "unitSerialNumberType": "PRODUCT_SET",
    }

    state["answer"] = introduce.Sent(
        "док-партия-40", introduce.DocStatus(code="CHECKED_OK")
    )
    tab.create_document()
    _settle(application)

    assert len(state["sent"]) == 1
    sent_doc, _, _, _ = state["sent"][0]
    assert len(sent_doc.units) == 40
    assert tab.codes == [] and tab.set_code == "" and tab.set_codes_pool == []


def test_загрузка_csv_с_кодами_продукции_вложений_в_разных_форматах(
    world, tmp_path
):
    tab, _, _ = world
    tab.set_set_code(SET_RAW)

    # CSV в кодировке Windows-1251 с заголовком, кавычками и удвоенными кавычками внутри КМ
    quoted_serial_ki = f'01{ITEM1_GTIN}21SER"Q1'
    quoted_serial_raw = f'{quoted_serial_ki}\x1d91EE06\x1d92dGVzdC1pdGVtLWNyeXB0by0x'
    csv_escaped = quoted_serial_raw.replace('"', '""')

    items_csv = tmp_path / "items.csv"
    lines = [
        "Код маркировки;GTIN;Наименование",
        f'"{ITEM1_RAW}";{ITEM1_GTIN};"Шампунь 250 мл"',
        f'"{ITEM2_RAW}";{ITEM2_GTIN};"Бальзам 250 мл"',
        f'"{csv_escaped}";{ITEM1_GTIN};"Шампунь с кавычкой в серии"',
    ]
    items_csv.write_bytes("\r\n".join(lines).encode("cp1251"))

    added, repeats, broken = tab.load_file(str(items_csv))
    assert (added, repeats, broken) == (3, 0, 0)
    assert len(tab.codes) == 3
    assert sets.key_of(tab.codes[2]) == quoted_serial_ki


def test_опрос_статуса_после_создания_не_прерывается_на_устаревшее_api_v3(monkeypatch):
    from app.core.marking import session
    from app.core.marking.transport import MarkingError

    doc_list_attempt = 0

    def fake_get(system, path, *, contour=None, params=None, token=None):
        nonlocal doc_list_attempt
        if system == "trueapi4" and path == introduce.LIST_PATH:
            doc_list_attempt += 1
            if doc_list_attempt == 1:
                # В первую секунду после создания документ ещё не проиндексирован в /doc/list
                return {"results": []}
            return {
                "results": [
                    {
                        "number": "98b6583f-04a3-46bb-ad79-165e0fe9fff2",
                        "status": "CHECKED_OK",
                    }
                ]
            }
        if system == "trueapi4" and path.startswith("/doc/"):
            raise MarkingError("404 Not Found", status=404)
        if system == "trueapi" and path.startswith("/doc/"):
            raise MarkingError("Устаревшее API", status=400)
        raise AssertionError(f"Unexpected call: {system} {path}")

    monkeypatch.setattr(
        introduce,
        "submit",
        lambda *args, **kwargs: "98b6583f-04a3-46bb-ad79-165e0fe9fff2",
    )
    monkeypatch.setattr(session, "authorized", lambda fn: fn("TOK"))
    monkeypatch.setattr(transport, "get", fake_get)

    doc = _doc()
    sent = introduce.send(
        doc,
        "THUMB",
        "chemistry",
        Contour.SANDBOX,
        sleep=lambda sec: None,
        wait=6.0,
        interval=3.0,
    )
    assert sent.doc_id == "98b6583f-04a3-46bb-ad79-165e0fe9fff2"
    assert sent.status.done
    assert sent.status.code == "CHECKED_OK"
    assert doc_list_attempt == 2



