"""Commit ordering, replay and conflicts on a real isolated PostgreSQL."""
import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_server_migrations import database, load_api, migrate, psycopg

pytestmark = pytest.mark.skipif(not os.environ.get("RETAILCORE_TEST_DSN"),
                                reason="нужен тестовый PostgreSQL: RETAILCORE_TEST_DSN")


@pytest.fixture
def api(database, load_api):
    dsn, connection = database
    migrate(connection)
    main, client = load_api(dsn)
    security = importlib.import_module(main.__package__ + ".security")
    ident = connection.execute(
        "INSERT INTO app_user(login,password_hash,is_admin) VALUES ('sync-test','hash',TRUE)"
        " RETURNING id").fetchone()["id"]
    user = security.User(ident, "sync-test", "Alice", True, frozenset({"Alice"}))
    main.app.dependency_overrides[security.current_user] = lambda: user
    with client:
        yield SimpleNamespace(client=client, connection=connection, dsn=dsn,
                              main=main, security=security, user=user)


def create(api, **fields):
    result = api.client.post("/api/payments", json={"recipient": "Test", "amount": 100,
                                                  "responsible": "Alice", **fields})
    assert result.status_code == 201, result.text
    return result.json()


def head(api):
    result = api.client.get("/api/payments/changes/head")
    assert result.status_code == 200, result.text
    return result.json()


def poll(api, cursor, **fields):
    result = api.client.post("/api/payments/changes", json={
        "epoch": cursor["epoch"], "after": cursor["revision"],
        "access": cursor["access"], **fields})
    assert result.status_code == 200, result.text
    return result.json()


def test_delta_replays_create_move_and_delete_with_filter_membership(api):
    start = head(api)
    first, second = create(api), create(api)
    result = poll(api, start)
    assert {row["id"] for row in result["upserted"]} == {first["id"], second["id"]}
    assert result["metadata"]
    assert api.client.patch(f"/api/payments/{first['id']}", json={
        "pay_date": "2026-11-13", "status": "moved"}).status_code == 200
    assert api.client.delete(f"/api/payments/{second['id']}").status_code == 204
    delta = poll(api, result, selection={"statuses": ["planned"]})
    assert delta["upserted"] == []
    assert set(delta["removed_ids"]) == {first["id"], second["id"]}
    assert not poll(api, delta)["upserted"]


def test_move_does_not_force_metadata_reload_but_budget_edit_does(api):
    row = create(api)
    start = head(api)
    api.client.patch(f"/api/payments/{row['id']}", json={"pay_date": "2026-11-13"})
    delta = poll(api, start)
    assert len(delta["upserted"]) == 1 and not delta["metadata"]
    api.connection.execute("INSERT INTO budget(year,month,amount) VALUES (2026,11,1000)")
    updated = poll(api, delta)
    assert updated["metadata"] and updated["upserted"] == []


def test_initial_cursor_precedes_full_read_so_concurrent_changes_are_replayed(api):
    row = create(api)
    start = head(api)
    api.client.patch(f"/api/payments/{row['id']}", json={"amount": 250})
    snapshot = api.client.get("/api/payments").json()
    delta = poll(api, start)
    assert snapshot[0]["amount"] == delta["upserted"][0]["amount"] == 250


def test_pages_are_bounded_and_replayed_until_current(api):
    start = head(api)
    ids = {create(api)["id"] for _ in range(3)}
    found = set()
    cursor = start
    for _ in range(3):
        cursor = poll(api, cursor, limit=1)
        assert len(cursor["upserted"]) == 1
        found.update(row["id"] for row in cursor["upserted"])
    assert found == ids and not cursor["more"]


def test_uncommitted_and_rolled_back_events_are_invisible(api):
    start = head(api)
    api.connection.execute("BEGIN")
    api.connection.execute("INSERT INTO payment(recipient,amount) VALUES ('Uncommitted',100)")
    try:
        delta = poll(api, start)
        assert delta["revision"] == start["revision"] and not delta["upserted"]
    finally:
        api.connection.execute("ROLLBACK")
    assert poll(api, start)["revision"] == start["revision"]


def test_parallel_transactions_cannot_commit_out_of_cursor_order(api):
    start = head(api)
    inserted, attempted, release = Event(), Event(), Event()

    def first():
        with psycopg.connect(api.dsn) as connection:
            connection.execute("INSERT INTO payment(recipient,amount) VALUES ('First',100)")
            inserted.set()
            assert release.wait(timeout=10)

    def second():
        assert inserted.wait(timeout=5)
        with psycopg.connect(api.dsn) as connection:
            attempted.set()
            connection.execute("INSERT INTO payment(recipient,amount) VALUES ('Second',100)")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        try:
            assert attempted.wait(timeout=5)
            assert not poll(api, start)["upserted"]
        finally:
            release.set()
        a.result(timeout=10)
        b.result(timeout=10)
    delta = poll(api, start)
    assert {row["recipient"] for row in delta["upserted"]} == {"First", "Second"}
    events = api.connection.execute("SELECT revision FROM payment_change ORDER BY revision").fetchall()
    assert [row["revision"] for row in events] == [1, 2]


def test_stale_single_edit_is_rejected_without_overwriting_peer(api):
    row = create(api)
    first = api.client.patch(f"/api/payments/{row['id']}", json={
        "amount": 250, "expected_revision": row["sync_revision"]})
    assert first.status_code == 200
    stale = api.client.patch(f"/api/payments/{row['id']}", json={
        "amount": 999, "expected_revision": row["sync_revision"]})
    assert stale.status_code == 409
    assert api.client.get(f"/api/payments/{row['id']}").json()["amount"] == 250


def test_bulk_conflict_rolls_back_entire_move_and_returns_fresh_versions_on_success(api):
    a, b = create(api), create(api)
    expected = {str(row["id"]): row["sync_revision"] for row in (a, b)}
    api.client.patch(f"/api/payments/{b['id']}", json={"amount": 250})
    conflict = api.client.patch("/api/payments", json={
        "ids": [a["id"], b["id"]], "patch": {"pay_date": "2026-11-13"},
        "expected_revisions": expected})
    assert conflict.status_code == 409
    assert api.client.get(f"/api/payments/{a['id']}").json()["pay_date"] is None
    current = api.client.get(f"/api/payments/{b['id']}").json()
    expected[str(b["id"])] = current["sync_revision"]
    success = api.client.patch("/api/payments", json={
        "ids": [a["id"], b["id"]], "patch": {"pay_date": "2026-11-13"},
        "expected_revisions": expected})
    assert success.status_code == 200, success.text
    assert success.json()["changed"] == len(success.json()["updated"]) == 2
    assert all(row["sync_revision"] > expected[str(row["id"])]
               for row in success.json()["updated"])


def test_old_clients_can_still_patch_without_version_fields(api):
    row = create(api)
    assert api.client.patch(f"/api/payments/{row['id']}", json={"amount": 200}).status_code == 200
    assert api.client.patch("/api/payments", json={
        "ids": [row["id"]], "patch": {"status": "paid"}}).status_code == 200


def test_expired_cursor_epoch_and_permission_changes_require_new_snapshot(api):
    old = head(api)
    assert poll(api, {**old, "epoch": "different-database"})["reset"]
    api.connection.execute("UPDATE payment_sync_state SET revision=50001 WHERE singleton")
    assert poll(api, old)["reset"]
    latest = head(api)
    changed_user = api.security.User(api.user.id, "sync-test", "Alice", False, frozenset())
    api.main.app.dependency_overrides[api.security.current_user] = lambda: changed_user
    assert poll(api, latest)["reset"]


def test_attachment_and_cascade_deletion_are_in_the_feed(api):
    row = create(api)
    start = head(api)
    api.connection.execute("INSERT INTO payment_file(payment_id,name,stored_as) VALUES (%s,'test','synthetic')",
                           (row["id"],))
    attached = poll(api, start)
    assert attached["upserted"][0]["files"] == 1
    api.client.delete(f"/api/payments/{row['id']}")
    assert poll(api, attached)["removed_ids"] == [row["id"]]


def test_truncate_forces_reset_instead_of_silently_leaving_old_cards(api):
    create(api)
    start = head(api)
    api.connection.execute("TRUNCATE payment CASCADE")
    assert poll(api, start)["reset"]


def test_direction_changes_reset_membership_instead_of_patching_only_loaded_rows(api):
    start = head(api)
    api.connection.execute("INSERT INTO direction(code,title) VALUES ('sync-test','Test')")
    assert poll(api, start)["reset"]


def test_change_endpoints_require_authentication(api):
    api.main.app.dependency_overrides.clear()
    assert api.client.get("/api/payments/changes/head").status_code == 401
    assert api.client.post("/api/payments/changes", json={
        "epoch": "test", "after": 0, "access": "test"}).status_code == 401


def test_stale_delete_cannot_erase_a_peers_updated_card(api):
    row = create(api)
    api.client.patch(f"/api/payments/{row['id']}", json={"amount": 250})
    result = api.client.delete(
        f"/api/payments/{row['id']}?expected_revision={row['sync_revision']}")
    assert result.status_code == 409
    assert api.client.get(f"/api/payments/{row['id']}").json()["amount"] == 250


def test_changed_rows_have_recipient_specific_edit_rights(api):
    start = head(api)
    a, b = create(api, responsible="Alice"), create(api, responsible="Bob")
    user = api.security.User(api.user.id, "sync-test", "Alice", False, frozenset({"Alice"}))
    api.main.app.dependency_overrides[api.security.current_user] = lambda: user
    # Obtain the ordinary user's scope before reading its deltas.
    scope = head(api)
    delta = poll(api, {**start, "access": scope["access"]})
    rights = {row["id"]: row["editable"] for row in delta["upserted"]}
    assert rights == {a["id"]: True, b["id"]: False}


def test_two_desktop_windows_receive_moves_and_deletes_without_full_reload(api, monkeypatch):
    import json
    import time
    from datetime import date
    from urllib.parse import urlsplit
    from PySide6.QtWidgets import QApplication
    from app.core.payments import data, remote, transport
    from app.core.settings import AppSettings
    from app.ui import payments_page as module

    application = QApplication.instance() or QApplication([])
    row = create(api, pay_date="2026-11-12")
    calls = []

    def send(request):
        parts = urlsplit(request.full_url)
        path = parts.path + ("?" + parts.query if parts.query else "")
        calls.append((request.method, parts.path))
        result = api.client.request(request.method, path, content=request.data,
                                    headers={"Content-Type": "application/json"})
        if result.status_code >= 400:
            raise transport.ServerError(result.json().get("detail", "error"), result.status_code)
        return json.loads(result.content) if result.content else None

    monkeypatch.setattr(transport, "session", transport.Session(
        base_url="https://example.test", token="synthetic", login="sync-test", user_id=api.user.id))
    monkeypatch.setattr(transport, "_send", send)
    monkeypatch.setattr(data, "online", lambda: True)
    monkeypatch.setattr(data, "backend", lambda: remote)

    def wait(condition):
        end = time.monotonic() + 10
        while not condition() and time.monotonic() < end:
            application.processEvents()
            time.sleep(0.005)
        assert condition()

    windows = [module.PaymentsPage(AppSettings(), lambda *args: None) for _ in range(2)]
    try:
        for window in windows:
            monkeypatch.setattr(window, "_schedule_poll", lambda *args: None)
            window.show()
            window.reload()
        wait(lambda: all(window._loaded for window in windows))
        first, second = windows
        second.selected_day = date(2026, 11, 12)
        second.refresh_calendar()
        assert second.day_list.count() == 1
        calls.clear()
        first.move_payments([row["id"]], date(2026, 11, 13))
        wait(lambda: not first._move_pending)
        second._poll_changes()
        wait(lambda: not second._poll_inflight)
        assert second.rows[0].pay_date == date(2026, 11, 13)
        assert second.day_list.count() == 0
        assert ("GET", "/api/payments") not in calls
        api.client.delete(f"/api/payments/{row['id']}")
        second._poll_changes()
        wait(lambda: not second._poll_inflight)
        assert second.rows == [] and second._total == 0
        assert ("GET", "/api/payments") not in calls
    finally:
        for window in windows:
            window.close()
