"""API JSON accepts compressed and plain responses, including errors."""
import gzip
import io
import json
from email.message import Message
from urllib.error import HTTPError

import pytest

from app.core.payments import transport


def response(body, encoding=""):
    stream = io.BytesIO(body)
    stream.headers = Message()
    if encoding:
        stream.headers["Content-Encoding"] = encoding
    return stream


@pytest.mark.parametrize("compressed", [False, True])
def test_json_requests_accept_gzip_and_plain_responses(monkeypatch, compressed):
    payload = {"rows": [{"id": 7, "recipient": "Тестовый поставщик"}]}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    seen = []

    def urlopen(request, **kwargs):
        seen.append(request)
        return response(gzip.compress(body) if compressed else body,
                        "gzip" if compressed else "")

    monkeypatch.setattr(transport.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(transport, "session", transport.Session(
        base_url="https://example.test", token="synthetic"))

    assert transport.get("/api/payments") == payload
    assert seen[0].get_header("Accept-encoding") == "gzip"


def test_compressed_auth_error_preserves_detail_and_clears_session(monkeypatch):
    headers = Message()
    headers["Content-Encoding"] = "gzip"
    body = gzip.compress(json.dumps({"detail": "Session expired"}).encode())

    def urlopen(*args, **kwargs):
        raise HTTPError("https://example.test/api/payments", 401, "Unauthorized",
                        headers, io.BytesIO(body))

    monkeypatch.setattr(transport.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(transport, "session", transport.Session(
        base_url="https://example.test", token="synthetic"))
    with pytest.raises(transport.AuthError, match="Session expired"):
        transport.get("/api/payments")
    assert not transport.session.active


def test_401_for_old_request_does_not_log_out_new_session(monkeypatch):
    monkeypatch.setattr(transport, "session", transport.Session(
        base_url="https://example.test", token="old-synthetic-session"))

    def urlopen(*args, **kwargs):
        transport.session.token = "new-synthetic-session"
        raise HTTPError("https://example.test/api/payments", 401, "Unauthorized",
                        Message(), io.BytesIO(b'{"detail":"Old request expired"}'))

    monkeypatch.setattr(transport.urllib.request, "urlopen", urlopen)
    with pytest.raises(transport.AuthError, match="Old request expired"):
        transport.get("/api/payments")
    assert transport.session.active
    assert transport.session.token == "new-synthetic-session"


@pytest.mark.parametrize("body", [b"not-gzip", gzip.compress(b"[]")[:-5]])
def test_corrupt_or_truncated_gzip_is_reported_as_transport_failure(monkeypatch, body):
    monkeypatch.setattr(transport.urllib.request, "urlopen",
                        lambda *a, **k: response(body, "gzip"))
    with pytest.raises(transport.OfflineError):
        transport._send(transport.urllib.request.Request("https://example.test"))


def test_empty_success_response_remains_supported(monkeypatch):
    monkeypatch.setattr(transport.urllib.request, "urlopen",
                        lambda *a, **k: response(b""))
    assert transport._send(transport.urllib.request.Request("https://example.test")) is None
