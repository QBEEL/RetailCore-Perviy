"""Единый UUID-токен True API: запрос, выбор токена и срок действия.

Контракт: docs.crpt.ru/gismt/True_API/, разделы 1.5.1–1.5.2.
Обмен и подпись подменены; закрытый ключ и реальные токены не используются.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.marking import session, transport
from app.core.marking.models import Contour

CHALLENGE_ID = "a63ff582-b723-4da7-958b-453da27a6c62"
UUID_TOKEN = "123e4567-e89b-12d3-a456-426655440000"
OLD_TOKEN = "123e4567-e89b-12d3-a456-426655440001"


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(session, "session", session.Session())
    calls = {"get": [], "post": [], "signed": []}
    deadline = datetime.now(timezone.utc) + timedelta(hours=6)
    reply = {"token": "legacy.jwt.value", "uuidToken": UUID_TOKEN,
             "expireDate": deadline.isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    monkeypatch.setattr(session.crypto, "find", lambda thumbprint: session.crypto.Certificate(
        thumbprint=thumbprint, subject="CN=Тестовый пользователь, ИНН=1234567890", has_private_key=True))

    def sign(data, thumbprint, detached=False):
        calls["signed"].append((data, thumbprint, detached))
        return "attached-signature-base64"

    def get(system, path, **kwargs):
        calls["get"].append((system, path, kwargs))
        return {"uuid": CHALLENGE_ID, "data": "random-challenge"}

    def post(system, path, **kwargs):
        calls["post"].append((system, path, kwargs))
        return dict(reply)

    monkeypatch.setattr(session.crypto, "sign", sign)
    monkeypatch.setattr(session.transport, "get", get)
    monkeypatch.setattr(session.transport, "post", post)
    return calls, reply


def test_login_requests_uuid_and_uses_attached_signature(auth):
    calls, reply = auth
    result = session.sign_in("certificate", "1234567890", Contour.PRODUCTION, "Организация")
    system, path, request = calls["post"][0]
    assert (system, path) == ("trueapi", "/auth/simpleSignIn")
    assert request["contour"] is Contour.PRODUCTION
    assert request["body"] == {"uuid": CHALLENGE_ID, "data": "attached-signature-base64",
                               "inn": "1234567890", "unitedToken": True}
    assert calls["get"][0][:2] == ("trueapi", "/auth/key")
    assert calls["signed"] == [("random-challenge", "certificate", False)]
    assert result.token == UUID_TOKEN and result.token != reply["token"]
    expected = datetime.fromisoformat(reply["expireDate"].replace("Z", "+00:00"))
    assert result.expires_at == expected.astimezone().replace(tzinfo=None)


def test_uuid_only_response_is_accepted(auth):
    _, reply = auth
    reply.pop("token")
    assert session.sign_in("certificate").token == UUID_TOKEN


def test_uuid_in_old_field_is_also_accepted():
    assert session._token({"token": UUID_TOKEN}) == UUID_TOKEN


@pytest.mark.parametrize("value", ["legacy.jwt.value", "not-a-uuid", 123, [UUID_TOKEN]])
def test_invalid_uuid_is_rejected_without_exposing_tokens(value):
    with pytest.raises(transport.MarkingError) as caught:
        session._token({"uuidToken": value, "token": "secret.jwt.value"})
    assert "secret.jwt.value" not in str(caught.value)
    assert "legacy.jwt.value" not in str(caught.value)
    assert "UUID" in str(caught.value)


def test_jwt_only_response_does_not_silently_keep_old_authentication():
    with pytest.raises(transport.MarkingError, match="UUID"):
        session._token({"token": "legacy.jwt.value"})


@pytest.mark.parametrize("offset", [timezone.utc, timezone(timedelta(hours=3)),
                                    timezone(timedelta(hours=-5))])
def test_iso_expiry_preserves_timezone_and_takes_precedence_over_ttl(offset):
    deadline = (datetime.now(offset) + timedelta(minutes=7)).replace(microsecond=123000)
    result = session._expiry({"expireDate": deadline.isoformat(), "expires_in": 36000})
    assert result == deadline.astimezone().replace(tzinfo=None)


def test_millisecond_timestamp_remains_supported():
    deadline = (datetime.now(timezone.utc) + timedelta(minutes=7)).replace(microsecond=123000)
    result = session._expiry({"expireDate": int(deadline.timestamp() * 1000)})
    assert result == deadline.astimezone().replace(tzinfo=None)


@pytest.mark.parametrize("expiry", ["not-a-date", "2099-01-01T00:00:00", True, {},
                                    "2000-01-01T00:00:00Z"])
def test_bad_expiry_does_not_replace_existing_session(auth, expiry):
    _, reply = auth
    session.session.contour = Contour.SANDBOX
    session.session.token = OLD_TOKEN
    session.session.thumbprint = "old-certificate"
    session.session.inn = "1111111111"
    session.session.organisation = "Старая организация"
    session.session.expires_at = datetime.now() + timedelta(minutes=10)
    before = vars(session.session).copy()
    reply["expireDate"] = expiry

    with pytest.raises(transport.MarkingError):
        session.sign_in("new-certificate", "2222222222", Contour.PRODUCTION, "Новая организация")
    assert vars(session.session) == before


def test_invalid_token_does_not_mix_old_identity_with_new_contour(auth):
    _, reply = auth
    session.session.token = OLD_TOKEN
    session.session.contour = Contour.SANDBOX
    before = vars(session.session).copy()
    reply["uuidToken"] = "bad-token"
    with pytest.raises(transport.MarkingError):
        session.sign_in("certificate", contour=Contour.PRODUCTION)
    assert vars(session.session) == before


def test_expiring_session_renews_with_uuid_and_keeps_organisation(auth):
    calls, _ = auth
    session.session.thumbprint = "certificate"
    session.session.inn = "1234567890"
    session.session.organisation = "Организация"
    session.session.contour = Contour.PRODUCTION
    session.session.token = OLD_TOKEN
    session.session.expires_at = datetime.now() + timedelta(seconds=30)

    assert session.token() == UUID_TOKEN
    request = calls["post"][0][2]
    assert request["body"]["unitedToken"] is True
    assert request["body"]["inn"] == "1234567890"
    assert request["contour"] is Contour.PRODUCTION
    assert session.session.organisation == "Организация"


def test_401_refreshes_uuid_once_and_retries_with_new_token(auth):
    calls, _ = auth
    session.session.thumbprint = "certificate"
    session.session.token = OLD_TOKEN
    session.session.expires_at = datetime.now() + timedelta(hours=1)
    used = []

    def request(token):
        used.append(token)
        if token == OLD_TOKEN:
            raise transport.AuthRequired("expired", 401)
        return "ok"

    assert session.authorized(request) == "ok"
    assert used == [OLD_TOKEN, UUID_TOKEN]
    assert len(calls["post"]) == 1
    assert calls["post"][0][2]["body"]["unitedToken"] is True


def test_uuid_is_sent_in_bearer_header(monkeypatch):
    sent = []
    monkeypatch.setattr(transport, "_send", lambda request, tolerate:
                        sent.append(request) or {"results": []})
    transport.get("trueapi4", "/doc/list", contour=Contour.SANDBOX,
                  params={"pg": "cosmetics", "number": "doc-1"}, token=UUID_TOKEN)
    assert sent[0].get_header("Authorization") == "Bearer " + UUID_TOKEN
