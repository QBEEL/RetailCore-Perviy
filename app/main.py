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
from .ui.theme import STYLESHEET


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
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)

    window = MainWindow(AppSettings.load())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
