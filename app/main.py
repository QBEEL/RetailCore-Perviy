"""Точка входа приложения."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from . import APP_TITLE, __version__
from .core import appdata, updater
from .core.settings import AppSettings
from .core.vpn import VlessError, VpnError, VpnManager, load_link
from .core.vpn import parse as parse_vless
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

    # Туннель поднимается до входа, но сервер оплат он не касается: адрес
    # сервера стоит в исключениях системного прокси. Останавливается он в
    # `finally`, а не по сигналу выхода из Qt: при отказе от входа цикл событий
    # не запускается, и прокси остался бы прописанным в реестре.
    vpn = VpnManager()
    _start_vpn(vpn, settings)
    try:
        # Вход до главного окна: программа знает, кто правит общие оплаты и за
        # кем закреплены поставщики, и «неизвестный пользователь» ей не подходит.
        # Отказ от входа означает выход — окно даже не создаётся.
        from .ui.widgets.login_dialog import Start, start_session

        outcome = start_session(settings)
        if outcome == Start.QUIT:
            return 0

        window = MainWindow(settings, offline=outcome == Start.OFFLINE, vpn=vpn)
        window.show()
        return app.exec()
    finally:
        vpn.stop()


def _start_vpn(vpn: VpnManager, settings: AppSettings) -> None:
    """Возвращает настройки после сбоя и поднимает туннель, если он был включён.

    Ошибка запуска не мешает работе: причина остаётся в `vpn.last_error`, и
    главное окно покажет её, когда откроется.
    """
    if not vpn.supported:
        return
    vpn.recover()
    if not settings.vpn_enabled:
        return
    try:
        vpn.start(parse_vless(load_link()))
    except (VlessError, VpnError) as error:
        vpn.last_error = str(error)


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
