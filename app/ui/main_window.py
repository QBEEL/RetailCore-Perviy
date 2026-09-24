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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import APP_TITLE, __version__
from ..core import changelog
from ..core.settings import AppSettings
from . import icons
from .catalog_page import CatalogPage
from .history_page import HistoryPage
from .marking_page import MarkingPage
from .match_page import MatchPage
from .order_page import OrderPage
from .payments_page import PaymentsPage
from .profile_page import ProfilePage
from .price_page import PricePage
from .reports_page import ReportsPage
from .settings_page import SettingsPage
from ..core.payments import data as payments_data
from ..core.payments import transport
from .admin_page import AdminPage
from .suppliers_page import SuppliersPage
from .pages import PAGES, index_of
from .theme import Metrics, Palette
from .update_check import UpdateChecker
from .widgets.base_toggle import BaseToggle
from .widgets.common import fade_in
from .widgets.toast import ToastKind, ToastManager
from .widgets.update_dialog import UpdateDialog

# Номера страниц в стопке — по порядку PAGES. Считаются по коду, а не пишутся
# числами: новый раздел в середине списка иначе тихо сдвинул бы половину из них.
PAGE_PAYMENTS = index_of("payments")
PAGE_SUPPLIERS = index_of("suppliers")
PAGE_MARKING = index_of("marking")
PAGE_REPORTS = index_of("reports")
PAGE_ADMIN = index_of("admin")
PAGE_MATCH = index_of("match")
# Личный кабинет добавлен после списка, а не внутрь него: вставка в середину
# сдвинула бы все номера страниц и сочетания клавиш, к которым люди привыкли.
PAGE_PROFILE = len(PAGES)

# Разделы, которым нужен сервер. Без него они не показывают ничего полезного,
# а половина их кнопок ответила бы ошибкой на первое же нажатие. Маркировки
# здесь нет: она ходит в «Честный ЗНАК» напрямую и своей базой, а разбор кодов
# работает и вовсе без сети.
SERVER_PAGES = (PAGE_PAYMENTS, PAGE_SUPPLIERS, PAGE_REPORTS, PAGE_ADMIN)


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
    def __init__(self, settings: AppSettings, offline: bool = False) -> None:
        super().__init__()
        self.settings = settings
        # Запуск без сервера: вход был раньше и льготный срок ещё не вышел.
        # Работа с файлами продолжается, разделы общей базы закрыты.
        self.offline = offline
        # Выбранная в прошлый раз база — до того, как страницы прочитают
        # оплаты: иначе первое чтение ушло бы не туда, куда человек оставил.
        # Права проверяются здесь же: настройку могли принести с чужой машины,
        # а не администратору выгружать потом нечем — он остался бы со своими
        # оплатами наедине и без переключателя, которым это видно.
        payments_data.set_local_only(
            settings.payment_local_base and transport.session.is_admin)
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
        self.order_page = OrderPage(settings, self.notify, self.pages, self.plan_payment)
        self.price_page = PricePage(
            settings, self.notify, self.pages, self.show_supplier, self.plan_payment)
        self.payments_page = PaymentsPage(settings, self.notify, self.pages, self.show_supplier)
        self.suppliers_page = SuppliersPage(settings, self.notify, self.pages)
        self.marking_page = MarkingPage(settings, self.notify, self.pages)
        self.reports_page = ReportsPage(settings, self.notify, self.pages)
        self.catalog_page = CatalogPage(settings, self.notify, self.pages)
        self.history_page = HistoryPage(settings, self.notify, self.pages)
        self.settings_page = SettingsPage(settings, self.notify, self.pages, self._check_updates_now)
        self.admin_page = AdminPage(settings, self.notify, self.pages,
                                    self.payments_page.invalidate)
        self.profile_page = ProfilePage(settings, self.notify, self.pages,
                                        self.sign_out)
        for page in (self.match_page, self.order_page, self.price_page, self.payments_page,
                     self.suppliers_page, self.marking_page, self.reports_page,
                     self.catalog_page, self.history_page, self.settings_page,
                     self.admin_page, self.profile_page):
            self.pages.addWidget(_scrollable(page))

        layout.addWidget(self._sidebar(root))
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)

        self.toasts = ToastManager(self)
        self.match_page.busy_changed.connect(self._set_busy)
        self._build_status_bar()
        self._build_actions()
        self._restore_geometry()
        self._lock_server_pages()
        self._sync_page_access()
        self.match_page.restore()
        self.pages.currentChanged.connect(self._on_page_changed)
        if self.offline:
            QTimer.singleShot(600, lambda: self.notify(
                "Сервер недоступен. Сопоставление, заказ и переоценка работают; "
                "оплаты, поставщики и отчётность откроются после входа.",
                ToastKind.WARNING))

        self.update_checker = UpdateChecker(self)
        self.update_dialog = UpdateDialog(settings, self.update_checker, self)
        QTimer.singleShot(800, self._show_whats_new)
        if settings.update_check_auto:
            QTimer.singleShot(2000, self.update_dialog.run_silent)

    def _show_whats_new(self) -> None:
        """После обновления — список изменений новой версии, один раз.

        Окно обновления рисует ещё старая версия, и что оно покажет, от новой
        не зависит. Поэтому новая версия рассказывает о себе сама.
        """
        seen = self.settings.seen_version
        if seen == __version__:
            return
        self.settings.seen_version = __version__
        self.settings.save()
        if changelog.is_upgrade(seen, __version__) and (lines := changelog.bundled(__version__)):
            self.update_dialog.show_whats_new(__version__, lines)

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
        version.setContentsMargins(8, 0, 0, 8)
        layout.addWidget(version)

        # Какая база в работе. Видно всегда, потому что перепутать их дорого:
        # оплаты, заведённые «не там», обнаруживаются только когда их
        # хватились. Переключатель — администратору, остальные работают в
        # общей базе всегда, и выбирать им не из чего.
        self._base_row = QWidget(bar)
        base_row_layout = QHBoxLayout(self._base_row)
        base_row_layout.setContentsMargins(0, 0, 0, 12)
        base_row_layout.setSpacing(6)

        self._base_toggle = BaseToggle(self._base_row)
        self._base_toggle.setToolTip(
            "Какая база в работе — общая на сервере или локальная на этом компьютере")
        self._base_toggle.toggled.connect(self.set_local_base)
        base_row_layout.addWidget(self._base_toggle, 1)

        self._upload_button = QToolButton(self._base_row)
        self._upload_button.setObjectName("BaseUploadButton")
        self._upload_button.setIcon(icons.icon("export"))
        self._upload_button.setToolTip("Выгрузить локальные оплаты на сервер…")
        self._upload_button.clicked.connect(self.upload_local_base)
        self._upload_button.setVisible(False)
        base_row_layout.addWidget(self._upload_button)

        layout.addWidget(self._base_row)
        self._sync_base_button(animate=False)

        self._nav_group = QButtonGroup(bar)
        self._nav_group.setExclusive(True)
        for index, page in enumerate(PAGES):
            button = QPushButton(f"  {page.title}", bar)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setIcon(icons.icon(page.icon))
            button.setToolTip(f"{page.title} ({page.shortcut})")
            button.clicked.connect(lambda _=False, i=index: self.show_page(i))
            self._nav_group.addButton(button, index)
            layout.addWidget(button)
            if index == PAGE_ADMIN:
                # Раздел появляется, только когда в общую базу вошёл
                # администратор. Показывать кнопку, которая ответит «нет прав»,
                # незачем — это не подсказка, а помеха.
                self._admin_button = button
                button.setVisible(False)

        layout.addStretch(1)

        # Личный кабинет — внизу, отдельно от разделов работы: это не ещё одна
        # задача, а «кто я здесь». Подпись показывает имя вошедшего, потому что
        # база общая и перепутать учётку легко.
        self._profile_button = QPushButton("", bar)
        self._profile_button.setObjectName("NavButton")
        self._profile_button.setCheckable(True)
        self._profile_button.setIcon(icons.icon("admin"))
        self._profile_button.setToolTip("Личный кабинет (Ctrl+P)")
        self._profile_button.clicked.connect(lambda: self.show_page(PAGE_PROFILE))
        self._nav_group.addButton(self._profile_button, PAGE_PROFILE)
        layout.addWidget(self._profile_button)
        self._sync_profile_button()

        hint = QLabel("F5 — сопоставить\nCtrl+S — сохранить\nCtrl+F — поиск", bar)
        hint.setObjectName("BrandSub")
        hint.setContentsMargins(8, 0, 0, 0)
        layout.addWidget(hint)
        return bar

    def _sync_base_button(self, animate: bool = True) -> None:
        """Положение переключателя базы. Не администратору его показывать не за чем."""
        allowed = transport.session.active and transport.session.is_admin
        self._base_row.setVisible(allowed)
        if not allowed:
            return
        local = payments_data.local_only()
        self._base_toggle.set_local(local, animate=animate)
        was_visible = self._upload_button.isVisible()
        self._upload_button.setVisible(local)
        if animate and local and not was_visible:
            fade_in(self._upload_button)

    def set_local_base(self, local: bool) -> None:
        """Переключает источник оплат и перечитывает то, что уже показано."""
        if local == payments_data.local_only():
            return
        payments_data.set_local_only(local)
        self.settings.payment_local_base = local
        self.settings.save()
        self._sync_base_button()
        self.payments_page.reload()
        self.notify(
            "Оплаты идут в локальную базу этого компьютера. Выгрузить их в "
            "общую можно кнопкой рядом с переключателем." if local else
            "Оплаты снова читаются и пишутся в общую базу отдела.",
            ToastKind.WARNING if local else ToastKind.SUCCESS)

    def upload_local_base(self) -> None:
        """Отправляет локальную базу в общую — с отчётом до записи."""
        from .widgets.sync_dialog import SyncDialog

        dialog = SyncDialog(parent=self)
        dialog.uploaded.connect(self._after_upload)
        dialog.exec()

    def _after_upload(self, report) -> None:  # type: ignore[no-untyped-def]
        self.notify(f"Выгрузка завершена: {report.summary}", ToastKind.SUCCESS)
        self.payments_page.reload()

    def _sync_profile_button(self) -> None:
        name = transport.session.full_name or transport.session.login
        self._profile_button.setText(f"  {name}" if name else "  Без сервера")

    def _lock_server_pages(self) -> None:
        """Гасит разделы общей базы, когда работаем без сервера.

        Кнопка остаётся видимой, но нажатие ничего не даёт: убрать её совсем —
        значит заставить человека гадать, куда делись оплаты, и решить, что
        программа сломалась.
        """
        if not self.offline:
            return
        for index in SERVER_PAGES:
            if button := self._nav_group.button(index):
                button.setEnabled(False)
                button.setToolTip("Недоступно без связи с сервером")

    def sign_out(self) -> None:
        """Выход из учётной записи. Работать без входа нельзя — окно закрывается."""
        from .widgets.login_dialog import sign_out as forget_session

        forget_session()
        self.close()

    def _sync_admin_access(self) -> None:
        """Показывает раздел администрирования, если вошёл администратор."""
        allowed = transport.session.active and transport.session.is_admin
        self._admin_button.setVisible(allowed)
        self._sync_profile_button()
        self._sync_base_button()
        self._sync_page_access()
        if not allowed and self.pages.currentIndex() == PAGE_ADMIN:
            # Вышли из общей базы, стоя на этом разделе: оставлять открытой
            # страницу, которой больше нет в меню, нельзя.
            self._open(self._first_open_page())

    def _sync_page_access(self) -> None:
        """Убирает из меню разделы, закрытые администратором этой учётке."""
        for index, page in enumerate(PAGES):
            if not page.managed:
                continue
            if button := self._nav_group.button(index):
                button.setVisible(transport.session.may_open(page.code))
        if not self._page_allowed(self.pages.currentIndex()):
            # Без уведомления: при запуске человек ничего не нажимал, а
            # объяснять закрытый раздел тому, кто его и не открывал, незачем.
            self._open(self._first_open_page())

    def _page_allowed(self, index: int) -> bool:
        if index >= len(PAGES):
            # Личный кабинет: туда ходят менять пароль и выходить из учётки, и
            # закрывать его нечем — он и есть выход из положения.
            return True
        page = PAGES[index]
        if index == PAGE_ADMIN:
            return transport.session.active and transport.session.is_admin
        return not page.managed or transport.session.may_open(page.code)

    def _first_open_page(self) -> int:
        """Куда отправить человека, когда открытая страница ему закрыта."""
        for index in range(len(PAGES)):
            if self._page_allowed(index):
                return index
        return PAGE_PROFILE

    def show_page(self, index: int) -> bool:
        """Открывает раздел. Ложь — он закрыт, и переход не состоялся."""
        if not self._page_allowed(index):
            # Молчать здесь нельзя: человек нажал Ctrl+M, ничего не произошло,
            # и решит, что программа сломалась, а не что раздел ему закрыли.
            self.notify(f"Раздел «{PAGES[index].title}» закрыт администратором",
                        ToastKind.WARNING)
            return False
        self._open(index)
        return True

    def _open(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        if button := self._nav_group.button(index):
            button.setChecked(True)

    def show_supplier(self, supplier_id: int) -> None:
        """Открывает карточку поставщика — переход с цен или с оплат."""
        if not self.show_page(PAGE_SUPPLIERS):
            return
        self.suppliers_page.show_supplier(supplier_id)

    def plan_payment(
        self,
        *,
        recipient: str = "",
        supplier_id: int = 0,
        amount: float = 0.0,
        terms_days: int = 0,
        comment: str = "",
        origin: str = "manual",
        origin_ref: str = "",
    ) -> None:
        """Переход «заказ или переоценка → оплата».

        Открывает вкладку оплат и сразу карточку новой записи: поставщик,
        комментарий и дата, посчитанная по отсрочке. Ничего не сохраняется —
        решение остаётся за пользователем.
        """
        if not self.show_page(PAGE_PAYMENTS):
            return
        self.payments_page.restore()
        self.payments_page.plan_from_module(
            recipient=recipient,
            supplier_id=supplier_id,
            amount=amount,
            terms_days=terms_days,
            comment=comment,
            origin=origin,
            origin_ref=origin_ref,
        )

    def _on_page_changed(self, index: int) -> None:
        page = _page_of(self.pages.widget(index))
        fade_in(self.pages.currentWidget(), 180)
        if page is self.catalog_page and self.match_page.source is not None:
            self.catalog_page.use_sheet(self.match_page.source)
        if page is self.payments_page:
            # База оплат читается при открытии: просрочка пересчитывается по
            # текущей дате, а импорт мог пройти на другой вкладке.
            self.payments_page.restore()
            # Вход выполняется здесь же, и права становятся известны только
            # после него — поэтому кнопку раздела проверяем следом.
            self._sync_admin_access()
        if page is self.admin_page:
            self.admin_page.restore()
        if page is self.profile_page:
            # Показатели читаются заново: закрепление меняет администратор, и
            # цифра, показанная при запуске, к обеду может устареть.
            self.profile_page.restore()
        if page is self.suppliers_page:
            # Список читается при каждом открытии: поставщик мог появиться,
            # пока пользователь сравнивал цены на соседней вкладке.
            self.suppliers_page.reload()
        if page is self.marking_page:
            # Сертификаты перечитываются при каждом открытии: носитель ключа
            # вставляют и вынимают, и список, прочитанный при запуске, может не
            # иметь отношения к тому, что подключено сейчас.
            self.marking_page.restore()
        if page is self.reports_page:
            # Профили и правила общие: коллега мог завести правило объединения,
            # пока вкладка была закрыта, и собирать отчёт по устаревшему набору
            # нельзя — расхождение всплывёт уже у поставщика.
            self.reports_page.restore()
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
        for index, page in enumerate(PAGES):
            self._add_action(page.shortcut, lambda i=index: self.show_page(i))
        self._add_action("F5", self._run_current)
        self._add_action(QKeySequence.StandardKey.Save, self._save_current)
        self._add_action(QKeySequence.StandardKey.Find, self._focus_search)
        self._add_action("Ctrl+O", self.match_page.source_picker.browse)
        self._add_action("Ctrl+Shift+O", self.match_page.target_picker.browse)
        self._add_action("Ctrl+R", self.match_page.open_last_session)
        self._add_action("Ctrl+P", lambda: self.show_page(PAGE_PROFILE))
        self._add_action("Ctrl+Q", self.close)

    def _add_action(self, shortcut, handler) -> None:
        action = QAction(self)
        action.setShortcut(QKeySequence(shortcut))
        action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        action.triggered.connect(handler)
        self.addAction(action)

    def _run_current(self) -> None:
        """F5 запускает то, что делает открытая страница, а не только сопоставление."""
        page = _page_of(self.pages.currentWidget())
        if page is self.price_page:
            self.price_page.run_comparison()
        elif page is self.order_page:
            self.order_page.run_transfer()
        elif page is self.payments_page:
            self.payments_page.reload()
        elif page is self.reports_page:
            self.reports_page.run_build()
        elif page is self.marking_page:
            # Страница решает сама: подвкладок на ней три, и «запустить» на
            # каждой значит своё.
            self.marking_page.run_current()
        elif self.show_page(PAGE_MATCH):
            self.match_page.run_matching()

    def _save_current(self) -> None:
        page = _page_of(self.pages.currentWidget())
        if page is self.price_page:
            self.price_page.export()
        elif page is self.order_page:
            self.order_page.save()
        elif page is self.reports_page:
            self.reports_page.save()
        elif page is self.marking_page:
            self.marking_page.save_current()
        else:
            self.match_page.save_results()

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
        elif page is self.price_page:
            self.price_page.search.setFocus()
            self.price_page.search.selectAll()
        elif page is self.payments_page:
            self.payments_page.focus_search()
        elif page is self.suppliers_page:
            self.suppliers_page.focus_search()

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
