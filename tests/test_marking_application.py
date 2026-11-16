"""Отчёт о нанесении КИН: полные коды, подпись и строгий статус документа."""
from __future__ import annotations

import json

import pytest

from app.core.marking import application, issue, transport
from app.core.marking.codes import GS
from app.core.marking.models import Contour
from app.core.marking.suz import Credentials

OMS = "b1a2c3d4-0000-1111-2222-333344445555"
REPORT = "3179f5d2-2bf5-47d1-8df0-9452b257d851"
GTIN = "04601234567893"
FULL = f"01{GTIN}21SET001{GS}91EE11{GS}92c3ludGhldGlj"
CREDS = Credentials(oms_id=OMS, connection_id="device", token="synthetic-token")


def _batch(**changes):
    batch = issue.Batch(id="synthetic", contour=Contour.SANDBOX.value,
                        gtin=GTIN, product_group="cosmetics", cis_type="SET",
                        codes=[FULL], application_status="SENDING")
    for key, value in changes.items():
        setattr(batch, key, value)
    return batch


def test_report_uses_full_code_and_detached_signature(monkeypatch):
    calls = []
    monkeypatch.setattr(application.crypto, "sign",
                        lambda body, thumbprint, detached: calls.append(
                            (body, thumbprint, detached)) or "synthetic-signature")
    def post(system, path, **kwargs):
        calls.append((system, path, kwargs))
        return {"omsId": OMS, "reportId": REPORT}
    monkeypatch.setattr(application.transport, "post", post)

    assert application.send(_batch(), CREDS, "synthetic-cert", Contour.SANDBOX) == REPORT
    body, thumbprint, detached = calls[0]
    assert (thumbprint, detached) == ("synthetic-cert", True)
    assert json.loads(body) == {"productGroup": "chemistry", "sntins": [FULL]}
    system, path, kwargs = calls[1]
    assert (system, path, kwargs["retries"]) == ("suz", "/utilisation", 0)
    assert kwargs["text_body"] == body
    assert kwargs["headers"]["X-Signature"] == "synthetic-signature"


@pytest.mark.parametrize("changes", [
    {"codes": [f"01{GTIN}21SET001"]},
    {"cis_type": "UNIT"},
    {"codes": [FULL, FULL]},
    {"application_status": "SUCCESS"},
    {"application_report_id": REPORT},
])
def test_report_rejects_unsafe_batch_before_network(monkeypatch, changes):
    monkeypatch.setattr(application.transport, "post",
                        lambda *_a, **_kw: pytest.fail("unexpected network"))
    with pytest.raises(ValueError):
        application.send(_batch(**changes), CREDS, "synthetic-cert", Contour.SANDBOX)


def test_report_status_matches_requested_id_and_does_not_send(monkeypatch):
    calls = []
    def get(system, path, **kwargs):
        calls.append((system, path, kwargs))
        return {"omsId": OMS, "reportId": REPORT, "reportStatus": "SUCCESS"}
    monkeypatch.setattr(application.transport, "get", get)
    monkeypatch.setattr(application.transport, "post",
                        lambda *_a, **_kw: pytest.fail("unexpected send"))

    assert application.status(REPORT, CREDS, Contour.SANDBOX) == ("SUCCESS", "")
    assert calls[0][1] == "/report/info"
    assert calls[0][2]["params"]["reportId"] == REPORT


def test_partially_processed_report_stays_partial(monkeypatch):
    monkeypatch.setattr(application.transport, "get", lambda *_a, **_kw: {
        "omsId": OMS, "reportId": REPORT, "reportStatus": "PARTIALLY",
        "errorReason": "synthetic partial failure"})
    assert application.status(REPORT, CREDS, Contour.SANDBOX) == (
        "PARTIALLY", "synthetic partial failure")


@pytest.mark.parametrize("response", [
    {"omsId": OMS, "reportId": "11111111-1111-1111-1111-111111111111",
     "reportStatus": "SUCCESS"},
    {"omsId": OMS, "reportId": REPORT, "reportStatus": "NEW_STATUS"},
    {"omsId": OMS, "reportId": REPORT},
])
def test_report_status_never_accepts_wrong_or_unknown_answer(monkeypatch, response):
    monkeypatch.setattr(application.transport, "get", lambda *_a, **_kw: response)
    with pytest.raises(transport.MarkingError):
        application.status(REPORT, CREDS, Contour.SANDBOX)
