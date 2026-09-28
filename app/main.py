"""Точка входа приложения."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from . import APP_TITLE, __version__
from .core import appdata, updater
from .core.settings import AppSettings
from .ui import icons
from .ui.main_window import MainWindow
from .ui.theme import STYLESHEET, light_palette


def _set_taskbar_identity() -> None:
    """Отдельный идентификатор, иначе Windows берёт иконку от python.exe."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RetailCore.App")
    except (AttributeError, OSError):
        pass


def main() -> int:
    # Настройки и история переезжают до первого обращения к ним: иначе
    # приложение стартовало бы с пустыми настройками после переименования.
    appdata.adopt_legacy_data()
    if updater.is_frozen():
        updater.cleanup_leftover()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    _set_taskbar_identity()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("RetailCore")
    app.setWindowIcon(icons.app_icon())
    # Оформление у приложения одно — светлое. Тёмная тема системы до него не
    # доходит: иначе Qt подставляет тёмную палитру, и всё, что таблица стилей
    # не закрашивает явно, темнеет (так было на Mac). Схема задаётся до
    # палитры: она же переключает на светлые системные окна выбора файла.
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    app.setStyle("Fusion")
    app.setPalette(light_palette())
    app.setStyleSheet(STYLESHEET)

    settings = AppSettings.load()
    _adopt_profiles(settings)

    # Вход до главного окна: программа знает, кто правит общие оплаты и за кем
    # закреплены поставщики, и «неизвестный пользователь» ей не подходит.
    # Отказ от входа означает выход — окно даже не создаётся.
    from .ui.widgets.login_dialog import Start, start_session

    outcome = start_session(settings)
    if outcome == Start.QUIT:
        return 0

    window = MainWindow(settings, offline=outcome == Start.OFFLINE)
    window.show()
    return app.exec()


def _adopt_profiles(settings: AppSettings) -> None:
    """Переносит профили поставщиков из settings.json в базу.

    Делается один раз, на пустой базе: профили появились раньше базы, и
    пользователь не должен настраивать соответствие колонок заново. Сбой
    переноса не мешает запуску — база просто останется пустой.
    """
    try:
        from .core import suppliers

        suppliers.adopt_settings_profiles(settings.supplier_profiles.items)
    except Exception:  # noqa: BLE001 — приложение обязано стартовать в любом случае
        pass


if __name__ == "__main__":
    sys.exit(main())
