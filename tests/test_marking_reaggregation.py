"""Частичное изъятие из КИТУ с подменой ЧЗ и УКЭП."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.marking import introduce, reaggregation, service, store
from app.core.marking.models import CodeInfo, CodeState, Contour, OperationKind, OperationStatus

BOX = "TEST-BOX-01"
ITEM = "010460700123001421REMOVEME"
LEFT = "010460700123001421LEAVEME"
INN = "7701234567"
CONTOUR = Contour.SANDBOX


@pytest.fixture
def fresh(monkeypatch):
    session = SimpleNamespace(contour=CONTOUR, inn=INN, thumbprint="test", owner="test")
    monkeypatch.setattr(service, "signed_in", lambda: True)
    monkeypatch.setattr(service, "current", lambda: session)
    box = CodeInfo(
        code=BOX, found=True, valid=True, owner_inn=INN, product_group="chemistry",
        package_type="BOX", state=CodeState.INTRODUCED, children=(ITEM, LEFT),
    )
    item = replace(box, code=ITEM, package_type="UNIT", children=(), raw={"cisInfo": {"parent": BOX}})
    answers = {BOX: box, ITEM: item, LEFT: replace(item, code=LEFT)}
    monkeypatch.setattr(service, "check_exact", lambda codes, _: [answers[code] for code in codes])
    return answers


def test_removal_document_and_journal(fresh, monkeypatch):
    calls = []

    def submit(document, thumbprint, group, contour, *, on_created):
        calls.append((json.loads(document.render()), document.wire_type(group), group))
        on_created("test-document")
        waiting, = store.pending()
        assert service.can_refresh(waiting)
        assert waiting.document_id == "test-document"
        return introduce.Sent("test-document", introduce.DocStatus("CHECKED_OK"))

    monkeypatch.setattr(introduce, "send", submit)
    operation = reaggregation.send(BOX, (ITEM,), INN, "test", CONTOUR)
    assert operation.status is OperationStatus.DONE
    assert store.get(operation.id).status is OperationStatus.DONE
    assert calls == [({
        "participant_inn": INN, "reaggregation_type": "REMOVING", "uitu": BOX,
        "uit_uitu_list": [{"uit_uitu": ITEM}],
    }, "REAGGREGATION_DOCUMENT", "chemistry")]


def test_pending_removal_blocks_another_request_for_same_package(fresh, monkeypatch):
    calls = []

    def submit(document, *_args, **_kwargs):
        calls.append(document.codes)
        raise introduce.IntroduceUnknown("Ответ не получен")

    monkeypatch.setattr(introduce, "send", submit)
    operation = reaggregation.send(BOX, (ITEM,), INN, "test", CONTOUR)
    assert operation.status is OperationStatus.SENDING
    with pytest.raises(ValueError, match="уже есть незавершённое изъятие"):
        reaggregation.send(BOX, (LEFT,), INN, "test", CONTOUR)
    assert calls == [(ITEM,)]


@pytest.mark.parametrize("change, message", [
    ({"owner_inn": "1111111111"}, "принадлежит вашей организации"),
    ({"package_type": "GROUP"}, "транспортной упаковки"),
    ({"children": (ITEM,)}, "расформирует упаковку"),
    ({"children": (LEFT,)}, "в составе упаковки"),
])
def test_parent_must_be_eligible(fresh, change, message):
    fresh[BOX] = replace(fresh[BOX], **change)
    with pytest.raises(ValueError, match=message):
        reaggregation.prepare(BOX, (ITEM,), INN, CONTOUR)


def test_rechecks_exact_parent_and_code_membership(fresh, monkeypatch):
    calls = []
    fresh[ITEM].raw["cisInfo"]["parent"] = "OTHER"

    def ask(codes, _):
        calls.append(tuple(codes))
        return [fresh[code] for code in codes]

    monkeypatch.setattr(service, "check_exact", ask)
    with pytest.raises(ValueError, match="не подтвердил привязку"):
        reaggregation.prepare(BOX, (ITEM,), INN, CONTOUR)
    assert calls == [(BOX, ITEM)]


def test_check_exact_sends_parent_without_normalizing(monkeypatch):
    raw_parent = "010460700123001413260724210000117"
    asked = []
    monkeypatch.setattr(service, "_ask", lambda batch, _: asked.extend(batch) or [])
    service.check_exact((raw_parent,), CONTOUR)
    assert asked == [raw_parent]


def test_locate_uses_current_parent_and_ignores_a_previous_link(fresh):
    fresh[BOX] = replace(fresh[BOX], package_type="SET", product_name="Тестовый набор")
    groups, packages, unknown = reaggregation.locate((ITEM,), INN, "test", CONTOUR)
    assert groups == {BOX: [ITEM]}
    assert packages[BOX].children == (ITEM, LEFT)
    assert not unknown

    fresh[ITEM] = replace(fresh[ITEM], raw={})
    assert reaggregation.locate((ITEM,), INN, "test", CONTOUR) == ({}, {}, ())


def test_disband_is_signed_once_and_pending_status_is_refreshed(fresh, monkeypatch):
    parent = replace(fresh[BOX], package_type="SET")
    fresh[BOX] = parent
    submissions = []

    def submit(document, thumbprint, group, contour, *, on_created):
        submissions.append((json.loads(document.render()), document.wire_type(group), thumbprint, contour))
        on_created("test-disband")
        return introduce.Sent("test-disband", introduce.DocStatus("IN_PROGRESS"))

    monkeypatch.setattr(introduce, "send", submit)
    operation = reaggregation.disband(BOX, (ITEM,), parent, INN, "test", CONTOUR)
    assert operation.kind is OperationKind.DISAGGREGATION
    assert operation.status is OperationStatus.SENT
    assert service.can_refresh(operation)
    with pytest.raises(ValueError, match="незавершённое расформирование"):
        reaggregation.disband(BOX, (LEFT,), parent, INN, "test", CONTOUR)
    assert submissions == [({"participant_inn": INN, "products_list": [{"uitu": BOX}]},
                            "DISAGGREGATION_DOCUMENT", "test", CONTOUR)]

    polled = []

    def status(doc_id, group, contour):
        polled.append((doc_id, group, contour))
        return introduce.DocStatus("CHECKED_OK")

    monkeypatch.setattr(introduce, "status", status)
    updated, = service.refresh_pending()
    assert updated.status is OperationStatus.DONE
    assert polled == [("test-disband", "chemistry", CONTOUR)]
    assert len(submissions) == 1


@pytest.mark.parametrize("change, message", [
    ({"owner_inn": "1111111111"}, "принадлежит вашей организации"),
    ({"state": CodeState.DISAGGREGATED}, "уже расформирован"),
    ({"package_type": "LEVEL1"}, "относится к набору"),
    ({"children": (ITEM,)}, "Состав или товарная группа набора изменились"),
    ({"raw": {"cisInfo": {"parent": "TEST-OUTER"}}}, "вложен в другую упаковку"),
])
def test_disband_rejects_an_ineligible_or_changed_parent(fresh, change, message):
    parent = replace(fresh[BOX], package_type="SET")
    fresh[BOX] = replace(parent, **change)
    with pytest.raises(ValueError, match=message):
        reaggregation.disband(BOX, (ITEM,), parent, INN, "test", CONTOUR)
    assert not store.pending()


def test_disband_rejects_a_changed_member_link(fresh):
    parent = replace(fresh[BOX], package_type="LEVEL1", raw={"cisInfo": {"generalPackageType": "SET"}})
    fresh[BOX] = parent
    fresh[ITEM] = replace(fresh[ITEM], raw={"cisInfo": {"parent": "TEST-OTHER"}})
    with pytest.raises(ValueError, match="Привязка проверяемого кода изменилась"):
        reaggregation.disband(BOX, (ITEM,), parent, INN, "test", CONTOUR)
    assert not store.pending()
