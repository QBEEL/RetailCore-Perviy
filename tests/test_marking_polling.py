"""Возобновление опроса документов по сохранённому журналу, без отправки в ГИС МТ."""
from datetime import datetime

import pytest

from app.core.marking import introduce, service, session, store, transport
from app.core.marking.models import Contour, Operation, OperationKind, OperationStatus


@pytest.fixture
def online(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "session", session.Session(
        contour=Contour.SANDBOX, token="test-token", thumbprint="test-certificate"))

    def forbidden(*args, **kwargs):
        pytest.fail("Опрос не должен подписывать или отправлять документ")

    monkeypatch.setattr(introduce, "submit", forbidden)
    monkeypatch.setattr(introduce.crypto, "sign", forbidden)
    return str(tmp_path / "marking.db")


def waiting(path, **changes):
    values = dict(kind=OperationKind.WITHDRAWAL, codes=["ki-1"], product_group="cosmetics",
                  status=OperationStatus.SENT, document_id="doc-1", contour=Contour.SANDBOX)
    values.update(changes)
    return store.create(Operation(**values), path)


@pytest.mark.parametrize("code,expected", [
    ("CHECKED_OK", OperationStatus.DONE),
    ("CHECKED_NOT_OK", OperationStatus.REJECTED),
    ("PROCESSING_ERROR", OperationStatus.REJECTED),
    ("IN_PROGRESS", OperationStatus.SENT),
    ("ACCEPTED", OperationStatus.SENT),
    ("UNDEFINED", OperationStatus.SENT),
    ("NEW_ERROR", OperationStatus.SENT),
    ("", OperationStatus.SENT),
])
def test_saved_document_is_updated_only_from_known_result(online, monkeypatch, code, expected):
    saved = waiting(online, error="прежний сбой сети")
    calls = []

    def ask(doc_id, group, contour):
        calls.append((doc_id, group, contour))
        return introduce.DocStatus(code, errors=("Подробности ответа",) if code == "CHECKED_NOT_OK" else ())

    monkeypatch.setattr(introduce, "status", ask)
    got = service.refresh_pending(online)

    assert calls == [("doc-1", "cosmetics", Contour.SANDBOX)]
    assert got[0].id == saved.id
    again = store.get(saved.id, online)
    assert again.status is expected and again.document_id == "doc-1"
    assert again.checked_at is not None
    assert not again.status.resendable
    if code == "CHECKED_OK":
        assert again.error == ""
    elif code == "CHECKED_NOT_OK":
        assert "Подробности ответа" in again.error


def test_other_contour_edo_missing_number_and_final_operations_are_skipped(online, monkeypatch):
    skipped = [
        waiting(online, contour=Contour.PRODUCTION),
        waiting(online, document_id=""),
        waiting(online, product_group=""),
        waiting(online, kind=OperationKind.SHIP),
        waiting(online, kind=OperationKind.ACCEPT),
        waiting(online, status=OperationStatus.DONE),
        waiting(online, status=OperationStatus.SENDING, document_id=""),
    ]
    eligible = waiting(online, document_id="poll-me")
    calls = []
    monkeypatch.setattr(introduce, "status", lambda doc_id, *args:
                        calls.append(doc_id) or introduce.DocStatus("CHECKED_OK"))

    service.refresh_pending(online)

    assert calls == ["poll-me"]
    assert store.get(eligible.id, online).status is OperationStatus.DONE
    for saved in skipped:
        assert store.get(saved.id, online).status is saved.status


def test_no_session_means_no_network(online, monkeypatch):
    saved = waiting(online)
    session.session.forget()
    monkeypatch.setattr(introduce, "status", lambda *args: pytest.fail("Вход не выполнен"))
    assert service.refresh_pending(online) == []
    assert store.get(saved.id, online).status is OperationStatus.SENT


def test_network_error_is_saved_without_stopping_other_documents(online, monkeypatch):
    first = waiting(online, document_id="offline")
    second = waiting(online, document_id="done")

    def ask(doc_id, *args):
        if doc_id == "offline":
            raise transport.Offline("нет связи")
        return introduce.DocStatus("CHECKED_OK")

    monkeypatch.setattr(introduce, "status", ask)
    progress = []
    service.refresh_pending(online, progress=lambda done, total: progress.append((done, total)))

    assert store.get(first.id, online).status is OperationStatus.SENT
    assert "нет связи" in store.get(first.id, online).error
    assert store.get(second.id, online).status is OperationStatus.DONE
    assert progress == [(1, 2), (2, 2)]


def test_authentication_failure_stops_polling_without_changing_sent_state(online, monkeypatch):
    saved = waiting(online)

    def ask(*args):
        raise transport.AuthRequired("нужен повторный вход")

    monkeypatch.setattr(introduce, "status", ask)
    with pytest.raises(transport.AuthRequired):
        service.refresh_pending(online)
    assert store.get(saved.id, online).status is OperationStatus.SENT


def test_concurrent_final_result_is_not_replaced_by_pending(online, monkeypatch):
    saved = waiting(online)

    def ask(*args):
        fresh = store.get(saved.id, online)
        fresh.status = OperationStatus.DONE
        fresh.comment = "сохранено параллельно"
        fresh.checked_at = datetime(2026, 11, 11, 10)
        store.update(fresh, online)
        return introduce.DocStatus("IN_PROGRESS")

    monkeypatch.setattr(introduce, "status", ask)
    result, = service.refresh_pending(online)
    assert result.status is OperationStatus.DONE
    assert result.comment == "сохранено параллельно"
    assert result.checked_at == datetime(2026, 11, 11, 10)


def test_polling_preserves_comment_changed_during_request(online, monkeypatch):
    saved = waiting(online)

    def ask(*args):
        fresh = store.get(saved.id, online)
        fresh.comment = "исправлено человеком"
        store.update(fresh, online)
        return introduce.DocStatus("CHECKED_OK")

    monkeypatch.setattr(introduce, "status", ask)
    service.refresh_pending(online)
    assert store.get(saved.id, online).comment == "исправлено человеком"
