"""Трей: крестик прячет окно, выйти можно только осознанно; копия одна.

Главное, что здесь стерегут: ни один настоящий выход (меню значка, Ctrl+Q, выход
из учётки, самообновление, завершение сеанса Windows) не должен превратиться в
«спрятали в трей» — иначе программа повисла бы невидимой, а обновление
оставило бы две копии.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import updater  # noqa: E402
from app.core.settings import AppSettings  # noqa: E402
from app.ui.settings_page import SettingsPage  # noqa: E402
from app.ui.single_instance import SingleInstance  # noqa: E402
from app.ui.tray import AppTray  # noqa: E402


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def restore_quit_policy(application):
    yield
    QApplication.setQuitOnLastWindowClosed(True)


@pytest.fixture
def tray_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """В тестовой среде области уведомлений нет — значок объявляется доступным."""
    monkeypatch.setattr(AppTray, "available", staticmethod(lambda: True))


def _window(settings: AppSettings | None = None):
    from app.ui.main_window import MainWindow

    window = MainWindow(settings or AppSettings(_path="unused.json"), offline=True)
    window.show()
    return window


def test_close_button_hides_window_instead_of_closing(application, tray_available) -> None:
    window = _window()
    assert window.tray is not None and window.tray.visible

    closed = window.close()

    assert closed is False, "закрытие отклонено"
    assert window.isHidden()
    assert not QApplication.quitOnLastWindowClosed()


def test_hiding_saves_window_state(application, tray_available, tmp_path: Path) -> None:
    settings = AppSettings(_path=str(tmp_path / "settings.json"))
    window = _window(settings)

    window.close()

    assert settings.window_geometry, "положение окна записано до того, как оно спряталось"
    assert (tmp_path / "settings.json").exists()


def test_tray_icon_returns_window(application, tray_available) -> None:
    window = _window()
    window.close()
    assert window.isHidden()

    window.tray.open_requested.emit()

    assert not window.isHidden()


def test_tray_is_announced_once_per_run(application, tray_available, monkeypatch) -> None:
    window = _window()
    shown: list[str] = []
    monkeypatch.setattr(window.tray, "message", lambda title, text, ms=0: shown.append(text))

    window.close()
    window.restore_window()
    window.close()

    assert len(shown) == 1


def test_real_quit_is_not_intercepted(application, tray_available) -> None:
    window = _window()

    window.quit_application()

    assert window.isHidden()
    assert window._quitting
    assert not window.tray.visible, "значок убирается вместе с программой"


def test_ctrl_q_really_quits(application, tray_available) -> None:
    window = _window()
    quit_action = next(a for a in window.actions() if a.shortcut().toString() == "Ctrl+Q")

    quit_action.trigger()

    assert window._quitting


def test_tray_menu_quit_really_quits(application, tray_available) -> None:
    window = _window()

    window.tray.quit_requested.emit()

    assert window._quitting


def test_sign_out_really_quits(application, tray_available, monkeypatch) -> None:
    from app.ui.widgets import login_dialog

    monkeypatch.setattr(login_dialog, "sign_out", lambda: None)
    window = _window()

    window.sign_out()

    assert window._quitting


def test_windows_session_end_does_not_hang_on_hidden_window(application,
                                                            tray_available) -> None:
    window = _window()

    window._session_ending()

    assert window.close() is True


def test_option_off_closes_as_before(application, tray_available) -> None:
    settings = AppSettings(_path="unused.json")
    settings.tray_on_close = False
    window = _window(settings)

    assert window.tray is None
    assert window.close() is True
    assert QApplication.quitOnLastWindowClosed()


def test_no_system_tray_closes_as_before(application, monkeypatch) -> None:
    monkeypatch.setattr(AppTray, "available", staticmethod(lambda: False))
    window = _window()

    assert window.tray is None
    assert window.close() is True


def test_toggling_option_in_settings_applies_at_once(application, tray_available,
                                                     monkeypatch) -> None:
    monkeypatch.setattr(AppTray, "supported", staticmethod(lambda: True))
    settings = AppSettings(_path="unused.json")
    window = _window(settings)
    page = window.settings_page
    assert page.tray_checkbox.isChecked()

    page.tray_checkbox.setChecked(False)
    assert not window.tray.visible
    assert window.close() is True


def test_claims_notice_goes_through_tray_icon(application, tray_available, monkeypatch) -> None:
    window = _window()
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(window.tray, "message",
                        lambda title, text, ms=0: shown.append((title, text)))

    window._system_notice("Торг-2", "Новая заявка")

    assert shown == [("Торг-2", "Новая заявка")]
    assert window._notice_tray is None, "второй значок рядом с постоянным не рисуется"


def test_update_closes_for_good_even_with_tray(application, tray_available) -> None:
    from app.ui.widgets.update_dialog import UpdateDialog

    window = _window()
    quit_calls: list[int] = []
    window.quit_application = lambda: quit_calls.append(1)  # type: ignore[method-assign]

    UpdateDialog._quit_for_update(window.update_dialog)

    assert quit_calls == [1]


# --- самообновление и вторая копия ---------------------------------------------------

def test_update_restart_is_marked_for_the_new_copy() -> None:
    environment = updater.restart_environment()

    assert environment[updater.RESTART_VARIABLE] == "1"
    assert updater.RESTART_VARIABLE not in updater.child_environment(
        {"PATH": "x"}), "обычный запуск маркера не получает"


def _name() -> str:
    return f"RetailCore.Test.{uuid.uuid4().hex}"


def _wait(condition, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while not condition() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)
    return condition()


def test_second_copy_does_not_start_and_wakes_the_first(application) -> None:
    """Вторая копия — настоящий отдельный процесс, как при двойном щелчке по ярлыку."""
    import subprocess

    name = _name()
    first = SingleInstance(name)
    woken: list[int] = []
    first.activated.connect(lambda: woken.append(1))
    assert first.claim() is True

    root = str(Path(__file__).resolve().parents[1])
    script = "; ".join([
        "import os, sys",
        "os.environ['QT_QPA_PLATFORM'] = 'offscreen'",
        f"sys.path.insert(0, {root!r})",
        "from PySide6.QtWidgets import QApplication",
        "from app.ui.single_instance import SingleInstance",
        "app = QApplication([])",
        f"print(SingleInstance({name!r}).claim())",
    ])
    process = subprocess.Popen([sys.executable, "-I", "-c", script],
                               stdout=subprocess.PIPE, text=True)
    assert _wait(lambda: process.poll() is not None, timeout=15.0)

    assert process.stdout.read().strip() == "False", "вторая копия не стартует"
    assert _wait(lambda: woken), "первая копия получила просьбу показать окно"


def test_request_survives_immediate_exit_of_the_second_copy(application) -> None:
    name = _name()
    first, second = SingleInstance(name), SingleInstance(name)
    woken: list[int] = []
    first.activated.connect(lambda: woken.append(1))
    assert first.claim() is True

    assert second.claim() is False

    assert _wait(lambda: woken)


def test_copy_after_update_waits_for_the_old_one_without_waking_it(application) -> None:
    name = _name()
    old, new = SingleInstance(name), SingleInstance(name)
    woken: list[int] = []
    old.activated.connect(lambda: woken.append(1))
    assert old.claim() is True

    # Старая копия закрывается через мгновение; цикл событий крутится у неё.
    import threading

    threading.Timer(0.5, lambda: old._server.close()).start()
    started = time.monotonic()

    assert new.claim(wait_for_previous=True, timeout=5.0) is True
    assert time.monotonic() - started < 4.0
    QApplication.processEvents()
    assert woken == [], "ожидание ухода не поднимает окно старой копии"


def test_copy_after_update_gives_up_if_old_one_hangs(application) -> None:
    name = _name()
    old, new = SingleInstance(name), SingleInstance(name)
    assert old.claim() is True

    assert new.claim(wait_for_previous=True, timeout=0.6) is False


def test_first_copy_alone_just_starts(application) -> None:
    assert SingleInstance(_name()).claim() is True
