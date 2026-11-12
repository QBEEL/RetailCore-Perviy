"""Incremental updates, reconnects and immutable edit expectations."""
import copy
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

from test_payments_loading import application, page, deferred, complete, payload, DAY, TARGET
from test_payments_dds import _server_row
from app.core.payments import Payment, PaymentStatus, data, remote, transport
from app.core.payments.live import Changes, Cursor
from app.ui import payments_page as module

START = Cursor("test-epoch", 1, "test-access")


@pytest.fixture
def live_page(page, monkeypatch):
    transport.session.base_url = "https://example.test"
    transport.session.token = "synthetic"
    page._metadata = payload().metadata
    page._metadata_source = module._source_key()
    page._sync_cursor = START
    page._loaded = True
    page.backend.changes_since = Mock(return_value=Changes(replace(START, revision=2)))
    monkeypatch.setattr(page, "isVisible", lambda: True)
    return page


def test_poll_merges_changes_removes_old_rows_and_recalculates_totals(live_page, deferred):
    live_page.rows.append(Payment(id=2, amount=200, recipient="Second", pay_date=DAY))
    new = Payment(id=1, amount=350, recipient="Test", pay_date=TARGET)
    live_page.backend.changes_since.return_value = Changes(
        replace(START, revision=3), [new], [2])
    live_page._poll_changes()
    complete(deferred[-1])
    assert live_page.rows == [new]
    assert live_page.stats.total == 350
    assert live_page._sync_cursor.revision == 3
    assert live_page.backend.list_payments.call_count == 0
    assert live_page.backend.budgets.call_count == 0


def test_reset_uses_fresh_snapshot(live_page, deferred):
    live_page.backend.changes_since.return_value = Changes(START, reset=True)
    live_page._poll_changes()
    complete(deferred[-1])
    assert len(deferred) == 2
    assert deferred[-1].function is module.directory.directions
    deferred[-1].result([])
    assert deferred[-1].function is module._load_all
    assert deferred[-1].args[1] is None


def test_only_one_poll_can_be_inflight(live_page, deferred):
    live_page._poll_changes()
    live_page._poll_changes()
    assert len(deferred) == 1


def test_reload_and_move_discard_old_poll_without_advancing_cursor(live_page, deferred):
    live_page._poll_changes()
    old = deferred[-1]
    live_page.move_payments([1], TARGET)
    old.result(module.PaymentDelta(Changes(replace(START, revision=99), [], [1])))
    assert live_page.rows[0].id == 1 and live_page._sync_cursor == START
    complete(deferred[-1])
    assert live_page.rows[0].pay_date == TARGET


def test_network_failure_preserves_cursor_and_retries_with_backoff(live_page, deferred):
    live_page._poll_changes()
    deferred[-1].error("Connection unavailable")
    assert live_page._sync_cursor == START
    assert live_page._poll_delay == 4000
    live_page._poll_changes()
    assert deferred[-1].args[0] == START
    complete(deferred[-1])
    assert live_page._sync_cursor.revision == 2
    assert live_page._poll_delay == 2000


def test_auth_failure_stops_polling_and_notifies_once(live_page, deferred):
    live_page._poll_changes()
    transport.session.clear()
    deferred[-1].error("Authentication required")
    assert live_page._sync_cursor is None
    assert not live_page._poll_inflight
    assert live_page.notify.call_count == 1
    assert live_page.rows == []  # Old remote IDs must never target local SQLite.


def test_changed_source_blocks_drag_of_old_remote_ids(live_page, deferred):
    transport.session.clear()
    live_page.move_payments([1], TARGET)
    assert live_page.backend.update_many.call_count == 0
    assert deferred[-1].function is module._load_all


def test_live_update_preserves_selection_of_unchanged_day_row(live_page, deferred):
    live_page.selected_day = DAY
    live_page.refresh_calendar()
    live_page.day_list.item(0).setSelected(True)
    live_page.backend.changes_since.return_value = Changes(
        replace(START, revision=2), [replace(live_page.rows[0], amount=200)])
    live_page._poll_changes()
    complete(deferred[-1])
    assert live_page.day_list.selected_ids() == [1]


def test_open_dialog_cannot_save_or_attach_after_session_change(live_page, monkeypatch):
    from app.ui.widgets import payment_dialogs
    monkeypatch.setattr(payment_dialogs, "run_task", lambda *a, **k: None)
    live_page.backend.current_user = lambda: "Alice"
    dialog = payment_dialogs.PaymentDialog(Payment(amount=100, recipient="Test"))
    transport.session.token = "another-session"
    dialog._accept()
    monkeypatch.setattr(payment_dialogs.QFileDialog, "getOpenFileNames",
                        lambda *a, **k: pytest.fail("attachment dialog must not open"))
    dialog._attach()
    assert dialog.result() != dialog.DialogCode.Accepted
    assert "Сеанс" in dialog.error.text()


@pytest.mark.parametrize("hidden,pending", [(True, False), (False, True)])
def test_hidden_or_saving_page_does_not_poll(live_page, deferred, monkeypatch, hidden, pending):
    monkeypatch.setattr(live_page, "isVisible", lambda: not hidden)
    live_page._move_pending = pending
    live_page._poll_changes()
    assert deferred == []


def test_metadata_changes_refresh_small_dictionaries_without_reading_payment_history(live_page, deferred):
    live_page.backend.budgets.return_value = [module.Budget(year=2026, month=11, amount=9000)]
    live_page.backend.changes_since.return_value = Changes(replace(START, revision=2), metadata=True)
    live_page._poll_changes()
    complete(deferred[-1])
    assert live_page._budgets[(2026, 11)].amount == 9000
    assert live_page.backend.list_payments.call_count == 0


def test_new_day_refreshes_overdue_and_metadata(live_page, deferred):
    live_page._metadata = replace(live_page._metadata, day=date.today() - timedelta(days=1))
    live_page._poll_changes()
    complete(deferred[-1])
    assert live_page.backend.refresh_overdue.call_count == 1
    assert live_page._metadata.day == date.today()


def test_large_replay_refreshes_metadata_only_after_last_page(live_page):
    middle = replace(START, revision=1000, metadata_dirty=True)
    live_page.backend.changes_since.return_value = Changes(middle, metadata=True, more=True)
    first = module._load_changes(START, module.Filter(), live_page._metadata, live_page.backend)
    assert first.metadata is None
    assert live_page.backend.budgets.call_count == 0
    live_page.backend.changes_since.return_value = Changes(replace(START, revision=1100))
    last = module._load_changes(middle, module.Filter(), live_page._metadata, live_page.backend)
    assert last.metadata is not None and live_page.backend.budgets.call_count == 1


def test_head_is_read_before_initial_data_to_avoid_missing_events(page, deferred):
    calls = []
    page.backend.changes_head = Mock(side_effect=lambda: calls.append("head") or START)
    page.backend.list_payments = Mock(side_effect=lambda *a, **k: calls.append("rows") or [])
    page.reload()
    complete(deferred[-1])
    assert calls == ["head", "rows"]
    assert page._sync_cursor == START


@pytest.mark.parametrize("status", [404, 405, 422])
def test_older_server_disables_subscription_without_breaking_loading(monkeypatch, status):
    monkeypatch.setattr(transport, "get", Mock(side_effect=transport.ServerError("old server", status)))
    assert remote.changes_head() is None


def test_remote_changes_preserve_filter_parameters_and_update_versions(monkeypatch):
    seen = []
    row = _server_row(id=1, sync_revision=8, editable=True)
    monkeypatch.setattr(transport, "post", lambda path, body: seen.append((path, body)) or {
        "epoch": START.epoch, "revision": 8, "access": START.access, "upserted": [row],
        "removed_ids": [], "metadata": False, "reset": False, "more": False})
    result = remote.changes_since(START, module.Filter(statuses=(PaymentStatus.PLANNED,),
                                                      start=DAY, dds_item="Test"))
    assert seen[0][1]["selection"]["statuses"] == ["planned"]
    assert seen[0][1]["selection"]["start"] == DAY
    assert result.upserted[0].sync_revision == remote.version_for(1) == 8


def test_move_captures_displayed_revision_even_if_an_inflight_poll_updates_cache(live_page, deferred):
    live_page.rows[0].sync_revision = 5
    live_page.backend.version_for = Mock(return_value=9)
    live_page.move_payments([1], TARGET)
    assert deferred[-1].kwargs["expected_revisions"] == {1: 5}


def test_remote_save_preserves_version_of_open_dialog_instead_of_newest_cached_version(monkeypatch):
    opened = remote._payment(_server_row(id=1, sync_revision=5, editable=True))
    remote._payment(_server_row(id=1, sync_revision=9, editable=True))
    seen = []
    monkeypatch.setattr(transport, "patch", lambda path, body: seen.append(body) or
                        _server_row(id=1, sync_revision=10, editable=True))
    remote.save_payment(opened)
    assert seen[0]["expected_revision"] == 5


def test_remote_bulk_uses_explicit_versions_and_remembers_server_confirmed_versions(monkeypatch):
    remote._payment(_server_row(id=1, sync_revision=9, editable=True))
    seen = []
    monkeypatch.setattr(transport, "patch", lambda path, body: seen.append(body) or {
        "changed": 1, "updated": [_server_row(id=1, sync_revision=10, editable=True)]})
    assert remote.update_many([1], pay_date=TARGET, expected_revisions={1: 5}) == 1
    assert seen[0]["expected_revisions"] == {1: 5}
    assert remote.version_for(1) == 10


def test_revision_cache_is_scoped_to_server_and_account(monkeypatch):
    monkeypatch.setattr(transport, "session", transport.Session(
        base_url="https://one.test", login="alice", user_id=1))
    remote._payment(_server_row(id=1, sync_revision=9, editable=True))
    assert remote.version_for(1) == 9
    transport.session.base_url = "https://two.test"
    assert remote.version_for(1) is None
    assert not remote.may_edit(1)


def test_discarded_old_response_cannot_reduce_confirmed_version():
    remote._payment(_server_row(id=1, sync_revision=10, editable=True))
    remote._payment(_server_row(id=1, sync_revision=5, editable=False))
    assert remote.version_for(1) == 10 and remote.may_edit(1)


def test_response_from_previous_login_cannot_seed_new_accounts_cache(monkeypatch):
    monkeypatch.setattr(transport, "session", transport.Session(
        base_url="https://example.test", login="alice", user_id=1, token="old"))
    owner = remote._owner()
    transport.session.token = "new"
    remote._payment(_server_row(id=1, sync_revision=10, editable=True), owner=owner)
    assert remote.version_for(1) is None


def test_new_database_epoch_clears_cached_versions_so_lower_versions_are_valid(monkeypatch):
    monkeypatch.setattr(transport, "get", lambda *a, **k: {
        "epoch": "replacement-database", "revision": 1, "access": "test"})
    remote._payment(_server_row(id=1, sync_revision=100, editable=True))
    remote.changes_head()
    remote._payment(_server_row(id=1, sync_revision=1, editable=True))
    assert remote.version_for(1) == 1
