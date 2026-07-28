"""Главное окно: боковая навигация, горячие клавиши, уведомления."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_TITLE, __version__
from ..core.settings import AppSettings
from . import icons
from .catalog_page import CatalogPage
from .history_page import HistoryPage
from .match_page import MatchPage
from .order_page import OrderPage
from .settings_page import SettingsPage
from .theme import Metrics, Palette
from .update_check import UpdateChecker
from .widgets.common import fade_in
from .widgets.toast import ToastKind, ToastManager
from .widgets.update_dialog import UpdateDialog

PAGES = (
    ("Сопоставление", "match", "Ctrl+1"),
    ("Заказ", "order", "Ctrl+2"),
    ("Каталог", "catalog", "Ctrl+3"),
    ("История данных", "history", "Ctrl+4"),
    ("Настройки", "settings", "Ctrl+5"),
)


def _scrollable(page: QWidget) -> QScrollArea:
    """Страница в прокручиваемой области.

    Без неё в невысоком окне (ноутбук 1366×768) карточки сжимаются ниже своего
    минимума и содержимое налезает друг на друга. Когда места хватает, область
    растягивает страницу и полосы прокрутки не появляются.
    """
    area = QScrollArea()
    area.setWidget(page)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setStyleSheet(
        "QScrollArea { background: transparent; border: none; }"
        " QScrollArea > QWidget > QWidget { background: transparent; }")
    return area


def _page_of(widget: QWidget | None) -> QWidget | None:
    return widget.widget() if isinstance(widget, QScrollArea) else widget


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self.settings = settings
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(1120, 700)
        self.setWindowIcon(icons.app_icon())

        root = QWidget(self)
        root.setObjectName("Root")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.pages = QStackedWidget(root)
        self.match_page = MatchPage(settings, self.notify, self.pages)
        self.order_page = OrderPage(settings, self.notify, self.pages)
        self.catalog_page = CatalogPage(settings, self.notify, self.pages)
        self.history_page = HistoryPage(settings, self.notify, self.pages)
        self.settings_page = SettingsPage(settings, self.notify, self.pages, self._check_updates_now)
        for page in (self.match_page, self.order_page, self.catalog_page,
                     self.history_page, self.settings_page):
            self.pages.addWidget(_scrollable(page))

        layout.addWidget(self._sidebar(root))
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)

        self.toasts = ToastManager(self)
        self.match_page.busy_changed.connect(self._set_busy)
        self._build_status_bar()
        self._build_actions()
        self._restore_geometry()
        self.match_page.restore()
        self.pages.currentChanged.connect(self._on_page_changed)

        self.update_checker = UpdateChecker(self)
        self.update_dialog = UpdateDialog(settings, self.update_checker, self)
        if settings.update_check_auto:
            QTimer.singleShot(2000, self.update_dialog.run_silent)

    # --- навигация ------------------------------------------------------------

    def _sidebar(self, parent: QWidget) -> QWidget:
        bar = QWidget(parent)
        bar.setObjectName("Sidebar")
        bar.setFixedWidth(Metrics.SIDEBAR_WIDTH)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(12, 18, 12, 14)
        layout.setSpacing(4)

        brand = QLabel(APP_TITLE, bar)
        brand.setObjectName("Brand")
        brand.setContentsMargins(8, 0, 0, 0)
        layout.addWidget(brand)
        version = QLabel(f"версия {__version__}", bar)
        version.setObjectName("BrandSub")
        version.setContentsMargins(8, 0, 0, 14)
        layout.addWidget(version)

        self._nav_group = QButtonGroup(bar)
        self._nav_group.setExclusive(True)
        for index, (title, icon_name, shortcut) in enumerate(PAGES):
            button = QPushButton(f"  {title}", bar)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setIcon(icons.icon(icon_name))
            button.setToolTip(f"{title} ({shortcut})")
            button.clicked.connect(lambda _=False, i=index: self.show_page(i))
            self._nav_group.addButton(button, index)
            layout.addWidget(button)

        layout.addStretch(1)
        hint = QLabel("F5 — сопоставить\nCtrl+S — сохранить\nCtrl+F — поиск", bar)
        hint.setObjectName("BrandSub")
        hint.setContentsMargins(8, 0, 0, 0)
        layout.addWidget(hint)
        return bar

    def show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        if button := self._nav_group.button(index):
            button.setChecked(True)

    def _on_page_changed(self, index: int) -> None:
        page = _page_of(self.pages.widget(index))
        fade_in(self.pages.currentWidget(), 180)
        if page is self.catalog_page and self.match_page.source is not None:
            self.catalog_page.use_sheet(self.match_page.source)
        if page is self.history_page:
            # Список читается при каждом открытии: снимок мог появиться,
            # пока пользователь работал на другой странице.
            self.history_page.reload()
        if page is self.settings_page:
            sheet = self.match_page.target or self.match_page.source or self.catalog_page.sheet
            if sheet is not None:
                self.settings_page.show_mapping(sheet)

    # --- статус и уведомления -------------------------------------------------

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        bar.setStyleSheet(
            f"QStatusBar {{ background: {Palette.SURFACE}; border-top: 1px solid {Palette.BORDER};"
            f" color: {Palette.TEXT_MUTED}; }}")
        self._busy = QProgressBar(self)
        self._busy.setRange(0, 0)
        self._busy.setFixedWidth(120)
        self._busy.setVisible(False)
        bar.addPermanentWidget(self._busy)
        bar.showMessage("Готово")

    def _set_busy(self, busy: bool) -> None:
        self._busy.setVisible(busy)
        self.statusBar().showMessage("Обработка…" if busy else "Готово")

    def notify(self, text: str, kind: ToastKind = ToastKind.INFO) -> None:
        self.toasts.show(text, kind)

    def _check_updates_now(self) -> None:
        self.update_dialog.run_visible()

    # --- горячие клавиши ------------------------------------------------------

    def _build_actions(self) -> None:
        for index, (_, _, shortcut) in enumerate(PAGES):
            self._add_action(shortcut, lambda i=index: self.show_page(i))
        self._add_action("F5", self._run_matching)
        self._add_action(QKeySequence.StandardKey.Save, self.match_page.save_results)
        self._add_action(QKeySequence.StandardKey.Find, self._focus_search)
        self._add_action("Ctrl+O", self.match_page.source_picker.browse)
        self._add_action("Ctrl+Shift+O", self.match_page.target_picker.browse)
        self._add_action("Ctrl+R", self.match_page.open_last_session)
        self._add_action("Ctrl+Q", self.close)

    def _add_action(self, shortcut, handler) -> None:
        action = QAction(self)
        action.setShortcut(QKeySequence(shortcut))
        action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        action.triggered.connect(handler)
        self.addAction(action)

    def _run_matching(self) -> None:
        self.show_page(0)
        self.match_page.run_matching()

    def _focus_search(self) -> None:
        page = _page_of(self.pages.currentWidget())
        if page is self.catalog_page:
            self.catalog_page.focus_search()
        elif page is self.match_page:
            self.match_page.table_search.setFocus()
            self.match_page.table_search.selectAll()
        elif page is self.order_page:
            self.order_page.search.setFocus()
            self.order_page.search.selectAll()

    # --- состояние окна -------------------------------------------------------

    def _restore_geometry(self) -> None:
        if self.settings.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(self.settings.window_geometry.encode()))
            return
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(1420, screen.width() - 120), min(880, screen.height() - 100))
        self.move(screen.center() - self.rect().center())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.toasts.reposition()

    def closeEvent(self, event) -> None:
        self.settings.window_geometry = self.saveGeometry().toBase64().data().decode()
        self.settings.splitter_sizes = self.match_page.splitter.sizes()
        self.settings.save()
        super().closeEvent(event)
