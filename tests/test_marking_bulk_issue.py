from types import SimpleNamespace
import pytest
from app.core.marking import bulk_issue, issue, service, transport
from app.core.marking.models import Contour


def test_partial_fetch_stops_without_retry(monkeypatch):
    order = SimpleNamespace(id="order", product_group="test", raw={}, buffers=[
        SimpleNamespace(gtin=g, left=5) for g in ("a", "b", "c")])
    calls = []

    def fetch(credentials, order_id, gtin, count, *args, **kwargs):
        calls.append(gtin)
        if gtin == "b":
            raise issue.CodesUnknown("Connection lost; recover block")
        return issue.Batch(id=gtin, codes=["code"]), credentials

    monkeypatch.setattr(service, "fetch_suz_codes", fetch)
    result, credentials, error = bulk_issue.fetch(
        "credentials", order, [(issue.Product(gtin=g), 1) for g in ("a", "b", "c")],
        Contour.SANDBOX, "", "")
    assert calls == ["a", "b"]
    assert [b.id for b in result] == ["a"]
    assert "b:" in error


def test_entire_plan_validated_before_first_request(monkeypatch):
    order = SimpleNamespace(buffers=[SimpleNamespace(gtin="a", left=5)])
    monkeypatch.setattr(service, "fetch_suz_codes", lambda *a, **k: pytest.fail("network"))
    with pytest.raises(ValueError):
        bulk_issue.fetch(None, order, [(issue.Product(gtin="a"), 1),
                                      (issue.Product(gtin="a"), 1)], Contour.SANDBOX, "", "")


def test_status_dialog_requests_fresh_data_and_does_not_guess_missing(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from app.ui.widgets import code_status_dialog as module
    from app.core.marking.models import CodeInfo, CodeState
    application = QApplication.instance() or QApplication([])
    current = SimpleNamespace(inn="test", thumbprint="cert", contour=Contour.SANDBOX)
    monkeypatch.setattr(service, "signed_in", lambda: True)
    monkeypatch.setattr(service, "current", lambda: current)
    callbacks = {}

    def task(function, codes, **kwargs):
        assert kwargs["use_cache"] is False
        callbacks.update(kwargs)

    monkeypatch.setattr(module, "run_task", task)
    dialog = module.CodeStatusDialog([issue.Batch(codes=["a", "b"])], Contour.SANDBOX)
    dialog.load()
    callbacks["on_result"]([CodeInfo(code="a", state=CodeState.EMITTED, found=True),
                            CodeInfo(code="b", found=False)])
    assert dialog.table.rowCount() == 2
    assert dialog.table.item(0, 1).text() == CodeState.EMITTED.title
    assert dialog.table.item(1, 1).text() != CodeState.EMITTED.title
    dialog.load()
    current.inn = "other"
    callbacks["on_result"]([CodeInfo(code="a", state=CodeState.INTRODUCED)])
    assert dialog.table.rowCount() == 0
    dialog.close()
