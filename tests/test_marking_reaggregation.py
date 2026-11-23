"""Частичное изъятие из КИТУ с подменой ЧЗ и УКЭП."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.marking import introduce, reaggregation, service, store
from app.core.marking.models import CodeInfo, CodeState, Contour, OperationStatus

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
