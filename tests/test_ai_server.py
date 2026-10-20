"""Серверная ИИ-правка: лимит, отказы Google, разбор ответа.

Серверные зависимости (fastapi, httpx и др.) на рабочем месте не нужны, поэтому без
них файл пропускается. Пакет сервера тоже называется `app`, как и клиентский, —
загружается под другим именем, чтобы они не спутались.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("jwt")
pytest.importorskip("psycopg_pool")

import httpx  # noqa: E402

SERVER_APP = Path(__file__).resolve().parents[1] / "server" / "api" / "app"


def _load():
    os.environ.setdefault("API_SECRET", "x" * 40)
    os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
    spec = importlib.util.spec_from_file_location(
        "server_app", SERVER_APP / "__init__.py", submodule_search_locations=[str(SERVER_APP)])
    package = importlib.util.module_from_spec(spec)
    sys.modules["server_app"] = package
    spec.loader.exec_module(package)
    from server_app import security  # noqa: PLC0415
    from server_app.routers import ai  # noqa: PLC0415
    return security, ai


security, ai = _load()

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

USER = security.User(id=7, login="km", full_name="КМ", is_admin=False, responsible=frozenset())


class Usage:
    """Счётчик обращений вместо базы: те же правила, что в SQL."""

    def __init__(self, limit: int) -> None:
        self.requests = 0
        self.limit = limit

    def fetch_one(self, query, params=None):
        if self.requests >= self.limit:
            return None
        self.requests += 1
        return {"requests": self.requests}

    def execute(self, query, params=None):
        self.requests = max(self.requests - 1, 0)
        return 1


def google(handler):
    """Gemini, отвечающий как задано: подменяет клиент httpx на тестовый."""
    real = httpx.Client

    def factory(**kwargs):
        kwargs.pop("proxy", None)
        return real(transport=httpx.MockTransport(handler), **kwargs)

    return factory


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(ai.router)
    app.dependency_overrides[security.current_user] = lambda: USER
    usage = Usage(limit=2)
    monkeypatch.setattr(ai.db, "fetch_one", usage.fetch_one)
    monkeypatch.setattr(ai.db, "execute", usage.execute)
    # Настройки заморожены, поэтому ключ ставится в обход запрета.
    object.__setattr__(ai.settings, "gemini_key", "secret-key")
    test = TestClient(app)
    test.usage = usage
    return test


def ok(text: str):
    return lambda request: httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})


BODY = {"instruction": "Сделай короче", "text": "Очень длинный текст заметки"}


class TestEdit:
    def test_returns_text_and_sends_key_in_header(self, client, monkeypatch) -> None:
        seen = {}

        def handler(request):
            seen["url"], seen["key"] = str(request.url), request.headers.get("x-goog-api-key")
            seen["body"] = request.content.decode()
            return ok("Коротко")(request)

        monkeypatch.setattr(ai.httpx, "Client", google(handler))
        response = client.post("/api/ai/edit", json=BODY)
        assert response.status_code == 200 and response.json() == {"text": "Коротко"}
        assert seen["key"] == "secret-key" and "secret-key" not in seen["url"]
        assert "Сделай короче" in seen["body"] and "Очень длинный" in seen["body"]

    def test_no_key_means_not_configured(self, client) -> None:
        object.__setattr__(ai.settings, "gemini_key", "")
        response = client.post("/api/ai/edit", json=BODY)
        assert response.status_code == 503 and "не настроен" in response.json()["detail"]
        assert client.usage.requests == 0

    def test_daily_limit(self, client, monkeypatch) -> None:
        monkeypatch.setattr(ai.httpx, "Client", google(ok("x")))
        assert client.post("/api/ai/edit", json=BODY).status_code == 200
        assert client.post("/api/ai/edit", json=BODY).status_code == 200
        third = client.post("/api/ai/edit", json=BODY)
        assert third.status_code == 429 and "Лимит" in third.json()["detail"]

    def test_google_failure_does_not_spend_the_limit(self, client, monkeypatch) -> None:
        monkeypatch.setattr(ai.httpx, "Client", google(
            lambda request: httpx.Response(500, json={"error": {"message": "boom"}})))
        assert client.post("/api/ai/edit", json=BODY).status_code == 502
        assert client.usage.requests == 0

    @pytest.mark.parametrize("code, message, expected", [
        (429, "quota", 503),
        (400, "User location is not supported for the API use", 502),
        (403, "API key not valid", 502),
    ])
    def test_google_errors_become_clear_answers(self, client, monkeypatch, code, message, expected) -> None:
        monkeypatch.setattr(ai.httpx, "Client", google(
            lambda request: httpx.Response(code, json={"error": {"message": message}})))
        response = client.post("/api/ai/edit", json=BODY)
        assert response.status_code == expected
        assert "secret-key" not in response.text and message not in response.text

    def test_blocked_prompt(self, client, monkeypatch) -> None:
        monkeypatch.setattr(ai.httpx, "Client", google(
            lambda request: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})))
        response = client.post("/api/ai/edit", json=BODY)
        assert response.status_code == 422 and client.usage.requests == 0

    def test_truncated_answer_is_not_returned(self, client, monkeypatch) -> None:
        monkeypatch.setattr(ai.httpx, "Client", google(lambda request: httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "обрывок"}]}, "finishReason": "MAX_TOKENS"}]})))
        assert client.post("/api/ai/edit", json=BODY).status_code == 422

    def test_thought_parts_are_dropped(self, client, monkeypatch) -> None:
        monkeypatch.setattr(ai.httpx, "Client", google(lambda request: httpx.Response(200, json={
            "candidates": [{"content": {"parts": [
                {"text": "рассуждение", "thought": True}, {"text": "итог"}]}}]})))
        assert client.post("/api/ai/edit", json=BODY).json() == {"text": "итог"}

    def test_network_failure(self, client, monkeypatch) -> None:
        def broken(request):
            raise httpx.ConnectError("secret proxy password")

        monkeypatch.setattr(ai.httpx, "Client", google(broken))
        response = client.post("/api/ai/edit", json=BODY)
        assert response.status_code == 502 and "password" not in response.text

    def test_timeout(self, client, monkeypatch) -> None:
        def slow(request):
            raise httpx.ReadTimeout("slow")

        monkeypatch.setattr(ai.httpx, "Client", google(slow))
        assert client.post("/api/ai/edit", json=BODY).status_code == 504

    @pytest.mark.parametrize("body", [
        {"instruction": "", "text": "текст"},
        {"instruction": "правь", "text": ""},
        {"instruction": "а" * 501, "text": "текст"},
        {"instruction": "правь", "text": "а" * 20001},
    ])
    def test_input_limits(self, client, body) -> None:
        assert client.post("/api/ai/edit", json=body).status_code == 422
        assert client.usage.requests == 0
