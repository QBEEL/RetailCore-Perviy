"""Loading, cache boundaries and moves with delayed server responses."""
import copy
import time
from dataclasses import replace
from datetime import date, timedelta
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.core.payments import Budget, Payment, PaymentStatus, data, transport
from app.core.settings import AppSettings
from app.ui import payments_page as module

DAY = date(2026, 11, 12)
TARGET = DAY + timedelta(days=1)


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(application, monkeypatch):
    rows = [Payment(id=1, amount=100, recipient="Test", pay_date=DAY)]
    backend = SimpleNamespace(
        refresh_overdue=Mock(return_value=0),
        list_payments=Mock(side_effect=lambda *a, **k: copy.deepcopy(rows)),
        known_values=Mock(return_value={"recipients": ["Test"]}),
        budgets=Mock(return_value=[Budget(year=2026, month=11, amount=1000)]),
        count_payments=Mock(return_value=1),
        unlinked_recipients=Mock(return_value=[]),
        update_many=Mock(return_value=1),
        may_edit=lambda payment: True,
    )
    monkeypatch.setattr(data, "backend", lambda: backend)
    monkeypatch.setattr(data, "online", lambda: True)
    monkeypatch.setattr(transport, "session", transport.Session())
    monkeypatch.setattr(module.dds, "catalog", Mock(return_value=[]))
    widget = module.PaymentsPage(AppSettings(), Mock())
    monkeypatch.setattr(widget, "_check_conflicts", lambda: None)
    widget.backend = backend
    widget.rows = copy.deepcopy(rows)
    widget._total = 1
    yield widget
    widget.close()


@pytest.fixture
def deferred(monkeypatch):
    jobs = []

    def enqueue(function, *args, on_result=None, on_error=None, **kwargs):
        jobs.append(SimpleNamespace(function=function, args=args, kwargs=kwargs,
                                    result=on_result, error=on_error))

    monkeypatch.setattr(module, "run_task", enqueue)
    return jobs


def complete(job):
    job.result(job.function(*job.args, **job.kwargs))


def payload(payment_id=1):
    metadata = module.PaymentMetadata({}, [], 1, 0, time.monotonic(), date.today())
    return module.PaymentData([Payment(id=payment_id, amount=100, recipient="Test")], metadata)


def wait_until(condition):
    end = time.monotonic() + 5
    while not condition() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)
    assert condition(), "background operation did not complete"


def test_all_loading_io_runs_outside_gui_thread(page, monkeypatch):
    gui_thread = get_ident()
    readers = ["refresh_overdue", "list_payments", "known_values", "budgets",
               "count_payments", "unlinked_recipients"]
    for name in readers:
        original = getattr(page.backend, name)

        def checked(*args, original=original, **kwargs):
            assert get_ident() != gui_thread, "backend accessed from GUI"
            return original(*args, **kwargs)

        monkeypatch.setattr(page.backend, name, checked)
    page.reload()
    wait_until(lambda: not page._loading)
    assert page._loaded
    assert page._total == 1 and page._budgets[(2026, 11)].amount == 1000
    assert page.notify.call_count == 0


def test_move_keeps_event_loop_responsive_and_updates_only_after_success(page, monkeypatch):
    entered, release = Event(), Event()
    heartbeat = []
    gui_thread = get_ident()

    def slow_write(*args, **kwargs):
        assert get_ident() != gui_thread
        entered.set()
        assert release.wait(timeout=5)
        return 1

    monkeypatch.setattr(page.backend, "update_many", slow_write)
    try:
        page.move_payments([1], TARGET)
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        wait_until(lambda: entered.is_set() and bool(heartbeat))
        assert page._move_pending
        assert page.rows[0].pay_date == DAY
        assert not page.grid.isEnabled()
    finally:
        release.set()
        wait_until(lambda: not page._move_pending)
    assert page.rows[0].pay_date == TARGET
    assert page.rows[0].status is PaymentStatus.MOVED
    assert page.grid.isEnabled()


def test_filters_reuse_metadata_but_explicit_refresh_reloads_it(page, deferred):
    page.reload()
    complete(deferred[-1])
    page.reload(force=False)
    complete(deferred[-1])
    assert page.backend.list_payments.call_count == 2
    for name in ("known_values", "budgets", "count_payments", "unlinked_recipients",
                 "refresh_overdue"):
        assert getattr(page.backend, name).call_count == 1
    assert module.dds.catalog.call_count == 1
    page.reload()
    complete(deferred[-1])
    assert page.backend.budgets.call_count == 2
    assert module.dds.catalog.call_count == 2


@pytest.mark.parametrize("interrupt", ["filter", "move"])
def test_forced_metadata_refresh_survives_interruption(page, deferred, interrupt):
    page.reload()
    complete(deferred[-1])
    page.reload()  # Explicit refresh is in flight; old metadata is still recent.
    forced = deferred[-1]
    if interrupt == "filter":
        page.reload(force=False)
    else:
        page.move_payments([1], TARGET)
        complete(deferred[-1])
    assert deferred[-1].args[1] is None
    complete(deferred[-1])
    forced.result(payload(99))
    assert page.backend.budgets.call_count == 2
    assert [p.id for p in page.rows] == [1]


@pytest.mark.parametrize("boundary", ["ttl", "day", "login", "server", "backend"])
def test_metadata_is_not_reused_across_expiry_or_source_changes(page, deferred, monkeypatch, boundary):
    page.reload()
    complete(deferred[-1])
    if boundary == "ttl":
        page._metadata = replace(page._metadata, loaded_at=time.monotonic() - 31)
    elif boundary == "day":
        page._metadata = replace(page._metadata, day=date.today() - timedelta(days=1))
    elif boundary == "login":
        transport.session.token = "another-synthetic-session"
    elif boundary == "server":
        transport.session.base_url = "https://other.example.test"
    else:
        monkeypatch.setattr(data, "backend", lambda: SimpleNamespace())
    page.reload(force=False)
    assert deferred[-1].args[1] is None


def test_late_response_and_error_cannot_overwrite_newer_filter_result(page, deferred):
    page.reload()
    old = deferred[-1]
    page.reload()
    new = deferred[-1]
    new.result(payload(2))
    old.result(payload(1))
    old.error("old timeout")
    assert [p.id for p in page.rows] == [2]
    assert page.notify.call_count == 0
    assert not page._loading


def test_result_of_previous_login_is_discarded(page, deferred):
    page.reload()
    transport.session.token = "new-synthetic-session"
    deferred[-1].result(payload(99))
    deferred[-1].error("previous account timeout")
    assert [p.id for p in page.rows] == [1]
    assert page.notify.call_count == 0


def test_current_auth_failure_finishes_loading_and_is_shown(page, deferred):
    transport.session.base_url = "https://example.test"
    transport.session.token = "expired-synthetic-session"
    page.reload()
    transport.session.clear()  # Transport clears an expired session on 401.
    deferred[-1].error("Authentication required")
    assert not page._loading
    assert page.notify.call_args.args[0] == "Authentication required"


def test_invalidation_discards_inflight_response(page, deferred):
    page.reload()
    old = deferred[-1]
    page.invalidate()
    old.result(payload(99))
    assert not page._loaded
    assert page._metadata is None
    assert not page._loading


def test_direction_lookups_are_reused_with_metadata(page, deferred, monkeypatch):
    directions = Mock(return_value=[])
    keys = Mock(return_value={})
    monkeypatch.setattr(module.directory, "directions", directions)
    monkeypatch.setattr(module.directory, "direction_keys", keys)
    selection = module.Filter(direction="none")
    first = module._load_all(selection, backend=page.backend)
    second = module._load_all(selection, first.metadata, page.backend)
    assert len(first.rows) == len(second.rows) == 1
    assert directions.call_count == keys.call_count == 1


def test_late_load_cannot_undo_move_and_triggers_fresh_read(page, deferred):
    page.reload()
    old = deferred[-1]
    page.move_payments([1], TARGET)
    move = deferred[-1]
    old.result(payload(99))
    assert [p.id for p in page.rows] == [1]
    complete(move)
    assert page.rows[0].pay_date == TARGET
    assert len(deferred) == 3  # The invalidated read is replaced after saving.
    old.result(payload(99))
    assert [p.id for p in page.rows] == [1]


def test_repeated_drag_is_blocked_and_filter_load_waits_for_save(page, deferred):
    page.move_payments([1], TARGET)
    move = deferred[-1]
    page.move_payments([1], TARGET + timedelta(days=1))
    page.reload(force=False)
    page.reload()
    assert len(deferred) == 1
    complete(move)
    assert page.backend.update_many.call_count == 1
    assert len(deferred) == 2
    assert deferred[-1].args[1] is None  # Explicit refresh is not lost.


def test_failed_move_keeps_card_and_allows_retry(page, deferred):
    page.move_payments([1], TARGET)
    deferred[-1].error("Connection unavailable")
    assert page.rows[0].pay_date == DAY
    assert page.grid.isEnabled() and not page._move_pending
    assert "Connection unavailable" in page.notify.call_args.args[0]
    page.move_payments([1], TARGET)
    complete(deferred[-1])
    assert page.rows[0].pay_date == TARGET


def test_move_response_from_previous_source_does_not_patch_current_cards(page, deferred):
    page.move_payments([1], TARGET)
    move = deferred[-1]
    transport.session.token = "another-synthetic-session"
    complete(move)
    assert page.rows[0].pay_date == DAY
    assert len(deferred) == 2
    assert not page._move_pending


def test_partial_move_response_causes_reconciliation(page, deferred):
    page.rows.append(Payment(id=2, amount=200, recipient="Second", pay_date=DAY))
    page.move_payments([1, 2], TARGET)
    complete(deferred[-1])  # Backend confirms only one of two records.
    assert len(deferred) == 2
    assert all(p.pay_date == DAY for p in page.rows)
