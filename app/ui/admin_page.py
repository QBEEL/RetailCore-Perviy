"""Администрирование: учётные записи и журнал изменений.

Раздел виден только администратору и только при работе с общей базой. Без входа
на сервер управлять нечем — учётные записи существуют там, а не в приложении.

Всё, что требует сети, уходит в фоновую задачу: сервер за границей офиса, и
замерший на секунду интерфейс выглядит как зависшая программа.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core.payments import admin, data, dds, transport
from ..core.payments.admin import Account, Entry, Scope
from ..core.suppliers import directory
from ..core.settings import AppSettings
from . import icons, pages
from .tasks import run_task
from .theme import Metrics, Palette
from .widgets.account_dialogs import AccountDialog, PasswordShown
from .widgets.common import Card, Hint, SectionTitle, Subtitle, Title
from .widgets import marks
from .widgets.database_dialogs import WipeDialog
from .widgets.journal_dialog import JournalEntryDialog
from .widgets.dds_dialog import DdsMarkDialog
from .widgets.table import Column, DataTable
from .widgets.toast import ToastKind


# Периоды отбора журнала: подпись и сколько дней назад начинать (None — всё время).
JOURNAL_PERIODS = (("За всё время", None), ("Сегодня", 0), ("За 7 дней", 7),
                   ("За 30 дней", 30))
JOURNAL_LIMITS = (200, 500, 1000, 2000)


def _read_journal(query: dict) -> list[Entry]:
    return admin.journal(**query)


def write_journal(entries: list[Entry], path: str) -> None:
    """Журнал в Excel — то, что видно в таблице, плюс пометки о проверке."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Журнал изменений"
    sheet.append(["Когда", "Кто", "Что", "Действие", "Запись", "Изменения", "Внимание"])
    for entry in entries:
        sheet.append([_moment(entry.at), entry.user, entry.entity, entry.action,
                      entry.record, entry.summary, "; ".join(entry.flags)])
    for column, width in zip("ABCDEFG", (18, 24, 18, 26, 52, 70, 34)):
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"
    book.save(path)


def _moment(value: datetime | None) -> str:
    return f"{value:%d.%m.%Y %H:%M}" if value else ""


def _version_text(account: Account) -> str:
    """«3.9.2 · Windows»; прочерк, если приложение себя не называет."""
    if not account.app_version:
        return "—"
    system = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}.get(
        account.app_platform, "")
    return f"{account.app_version} · {system}" if system else account.app_version


def _ai_usage_text(account: Account) -> str:
    """«3 из 20» — обращений к ИИ сегодня из предела; «закрыто» при нуле."""
    if account.ai_limit is None:
        return f"{account.ai_used} (общий)" if account.ai_used else "общий"
    if account.ai_limit == 0:
        return "закрыто"
    return f"{account.ai_used} из {account.ai_limit}"


class AdminPage(QWidget):
    """Страница администрирования."""

    def __init__(self, settings: AppSettings, notify,
                 parent: QWidget | None = None,
                 on_wiped: Callable[[], None] | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        # Кого предупредить, что оплат больше нет. Раздел оплат читает базу
        # один раз, и без этого он до перезапуска показывал бы стёртое.
        self._on_wiped = on_wiped
        self.accounts: list[Account] = []
        self.entries: list[Entry] = []
        self._known: list[str] = []
        self._directions: list[tuple[str, str]] = []
        self._claims: list[directory.Entry] = []
        self._dds_items: list[dds.DdsItem] = []
        self._scope = Scope()
        self._loaded = False
        # Номер последнего запроса журнала: ответы приходят не по порядку, и
        # медленный ответ на прежний отбор не должен затирать свежий.
        self._journal_request = 0
        self._journal_shown: list[Entry] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 8, Metrics.PAD + 4,
                                Metrics.PAD + 8, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(Title("Администрирование", self))
        self.subtitle = Subtitle("Учётные записи и журнал изменений общей базы", self)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)

        self.refresh_button = QPushButton("Обновить", self)
        self.refresh_button.setIcon(icons.icon("refresh"))
        self.refresh_button.clicked.connect(self.reload)
        head.addWidget(self.refresh_button)

        self.new_button = QPushButton("Новая учётная запись", self)
        self.new_button.setObjectName("Primary")
        self.new_button.setIcon(icons.icon("plus"))
        self.new_button.clicked.connect(self.create_account)
        head.addWidget(self.new_button)
        root.addLayout(head)

        self.offline = Card(self)
        self.offline.body().addWidget(SectionTitle("Нет связи с общей базой"))
        self.offline.body().addWidget(Hint(
            "Учётные записи хранятся на сервере. Откройте раздел «Оплаты» и "
            "войдите — после этого управление станет доступно."))
        root.addWidget(self.offline)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._accounts_tab(), "Учётные записи")
        self.tabs.addTab(self._claims_tab(), "Закрепление поставщиков")
        self.tabs.addTab(self._dds_tab(), "Статьи ДДС")
        self.tabs.addTab(self._journal_tab(), "Журнал изменений")
        self.tabs.addTab(self._maintenance_tab(), "Обслуживание")
        root.addWidget(self.tabs, 1)

    # --- вкладки --------------------------------------------------------------

    def _accounts_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, Metrics.GAP, 0, 0)
        layout.setSpacing(Metrics.GAP)

        self.accounts_table = DataTable([
            Column("ФИО", lambda a: a.title, 220),
            Column("Логин", lambda a: a.login, 140),
            Column("Роль", lambda a: a.role, 170,
                   color=lambda a: None if a.is_active else QColor(Palette.TEXT_FAINT)),
            Column("Направления", lambda a: ", ".join(a.directions), 140),
            Column("Разделы", lambda a: pages.describe(a.denied_pages), 220),
            Column("ИИ сегодня", _ai_usage_text, 110,
                   sort_key=lambda a: a.ai_used),
            Column("Имён в 1С", lambda a: len(a.responsible), 90,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            Column("Кто в 1С", lambda a: ", ".join(a.responsible), 320),
            Column("Версия", _version_text, 130,
                   color=lambda a: QColor(Palette.TEXT_FAINT) if not a.app_version else None,
                   sort_key=lambda a: admin.version_key(a.app_version)),
            Column("Был на связи", lambda a: _moment(a.app_seen_at), 130,
                   sort_key=lambda a: a.app_seen_at or datetime.min),
            Column("Заведена", lambda a: _moment(a.created_at), 130,
                   sort_key=lambda a: a.created_at or datetime.min),
        ], page)
        self.accounts_table.item_activated.connect(self.edit_account)
        # Кто уже перешёл на нынешнюю версию: пока этих людей не все, прежний адрес
        # обновлений нужен, и закрывать исходный репозиторий рано.
        self.versions = Hint("", page)
        layout.addWidget(self.versions)
        layout.addWidget(self.accounts_table, 1)

        actions = QHBoxLayout()
        self.edit_button = QPushButton("Изменить", page)
        self.edit_button.setIcon(icons.icon("edit"))
        self.edit_button.clicked.connect(lambda: self.edit_account(self._chosen()))
        actions.addWidget(self.edit_button)

        self.reset_button = QPushButton("Назначить новый пароль", page)
        self.reset_button.setObjectName("Ghost")
        self.reset_button.setIcon(icons.icon("key"))
        self.reset_button.clicked.connect(self.reset_password)
        actions.addWidget(self.reset_button)

        actions.addStretch(1)
        self.hint = QLabel("", page)
        self.hint.setObjectName("Hint")
        actions.addWidget(self.hint)
        layout.addLayout(actions)
        return page

    def _claims_tab(self) -> QWidget:
        """Очередь заявок: что менеджеры отметили своим и ждёт подтверждения."""
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, Metrics.GAP, 0, 0)
        layout.setSpacing(Metrics.GAP)

        layout.addWidget(Hint(
            "Менеджер отмечает своих поставщиков сам — до подтверждения они уже "
            "считаются его. Фиксация закрывает закрепление: снять его сможете "
            "только вы. Отклонённая заявка просто исчезает, менеджер может "
            "подать её снова."))

        self.claims_table = DataTable([
            Column("Поставщик", lambda e: e.recipient, 300, highlight=True),
            Column("Заявили", lambda e: ", ".join(
                p.full_name for p in e.assigned if not p.fixed), 260),
            Column("Направление", lambda e: ", ".join(e.directions), 130),
            Column("Оплат", lambda e: e.payments, 70,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            Column("Сумма", lambda e: f"{e.amount:,.0f}".replace(",", " "), 120,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   sort_key=lambda e: e.amount),
            Column("Уже закреплены", lambda e: ", ".join(
                p.full_name for p in e.assigned if p.fixed), 220),
        ], page)
        layout.addWidget(self.claims_table, 1)

        actions = QHBoxLayout()
        self.fix_button = QPushButton("Зафиксировать", page)
        self.fix_button.setObjectName("Primary")
        self.fix_button.setIcon(icons.icon("check", Palette.TEXT_ON_PRIMARY))
        self.fix_button.clicked.connect(self.fix_claims)
        actions.addWidget(self.fix_button)

        self.reject_button = QPushButton("Отклонить", page)
        self.reject_button.setObjectName("Danger")
        self.reject_button.setIcon(icons.icon("trash", Palette.DANGER))
        self.reject_button.setToolTip(
            "Снять заявку. Менеджер сможет подать её снова")
        self.reject_button.clicked.connect(self.reject_claims)
        actions.addWidget(self.reject_button)

        actions.addStretch(1)
        self.claims_hint = QLabel("", page)
        self.claims_hint.setObjectName("Hint")
        actions.addWidget(self.claims_hint)
        layout.addLayout(actions)
        return page

    def _dds_tab(self) -> QWidget:
        """Справочник статей ДДС: поиск по списку и добавление новой статьи."""
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, Metrics.GAP, 0, 0)
        layout.setSpacing(Metrics.GAP)

        layout.addWidget(Hint(
            "Из этого списка менеджер выбирает статью при создании оплаты. "
            "Новая статья появится у всех сразу. Удалить или переименовать "
            "статью нельзя: её название уже записано в оплатах. Кнопка «Цвет и "
            "метка» красит оплаты статьи: в таблице и календаре у них появляется "
            "цветной кружок, а рядом — метка, например «Маркетинг»."))

        add_row = QHBoxLayout()
        self.dds_new = QLineEdit(page)
        self.dds_new.setPlaceholderText("Название новой статьи")
        self.dds_new.setMaxLength(300)
        self.dds_new.returnPressed.connect(self.add_dds_item)
        add_row.addWidget(self.dds_new, 1)
        self.dds_add_button = QPushButton("Добавить статью", page)
        self.dds_add_button.setObjectName("Primary")
        self.dds_add_button.setIcon(icons.icon("plus"))
        self.dds_add_button.clicked.connect(self.add_dds_item)
        add_row.addWidget(self.dds_add_button)
        layout.addLayout(add_row)

        self.dds_search = QLineEdit(page)
        self.dds_search.setPlaceholderText("Поиск по списку статей")
        self.dds_search.setClearButtonEnabled(True)
        layout.addWidget(self.dds_search)

        self.dds_table = DataTable([
            Column("Статья ДДС", lambda item: item.title, 520, highlight=True,
                   icon=lambda item: _dot(item)),
            Column("Цвет", lambda item: _color_name(item), 120),
            Column("Метка", lambda item: item.note, 200,
                   color=lambda item: _ink(item)),
        ], page)
        self.dds_table.model_.set_row_tint(_tint)
        self.dds_search.textChanged.connect(self._filter_dds)
        layout.addWidget(self.dds_table, 1)

        actions = QHBoxLayout()
        self.dds_mark_button = QPushButton("Цвет и метка…", page)
        self.dds_mark_button.setIcon(icons.icon("edit"))
        self.dds_mark_button.setToolTip(
            "Покрасить выбранные статьи и задать метку. Выделить несколько — "
            "Ctrl или Shift")
        self.dds_mark_button.clicked.connect(self.mark_dds_items)
        actions.addWidget(self.dds_mark_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.dds_hint = QLabel("", page)
        self.dds_hint.setObjectName("Hint")
        layout.addWidget(self.dds_hint)
        return page

    def _journal_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, Metrics.GAP, 0, 0)
        layout.setSpacing(Metrics.GAP)

        layout.addLayout(self._journal_filters())

        self.journal_table = DataTable([
            Column("Когда", lambda e: _moment(e.at), 140,
                   sort_key=lambda e: e.at or datetime.min),
            Column("Кто", lambda e: e.user, 170),
            Column("Что", lambda e: e.entity, 110),
            Column("Действие", lambda e: e.action, 160),
            Column("Внимание", lambda e: "; ".join(e.flags), 200,
                   color=lambda e: QColor(Palette.WARNING) if e.flags else None,
                   tip="Что в этом действии стоит проверить"),
            Column("Запись", lambda e: e.record, 300),
            Column("Изменения", lambda e: e.summary, 380),
        ], page)
        self.journal_table.model_.set_row_tint(
            lambda e: marks.tint(QColor(Palette.WARNING)) if e.flags else None)
        self.journal_table.item_activated.connect(self.open_entry)
        layout.addWidget(self.journal_table, 1)
        return page

    def _journal_filters(self) -> QVBoxLayout:
        """Отбор журнала: кто, что, когда и поиск по тексту."""
        box = QVBoxLayout()
        box.setSpacing(Metrics.GAP // 2)

        top = QHBoxLayout()
        top.setSpacing(Metrics.GAP // 2)
        self.journal_search = QLineEdit(self)
        self.journal_search.setPlaceholderText("Поиск: получатель, менеджер, комментарий…")
        self.journal_search.setClearButtonEnabled(True)
        # Запрос уходит, когда человек договорил, а не на каждую букву.
        self._journal_timer = QTimer(self)
        self._journal_timer.setSingleShot(True)
        self._journal_timer.setInterval(500)
        self._journal_timer.timeout.connect(self.reload_journal)
        self.journal_search.textChanged.connect(lambda _: self._journal_timer.start())
        top.addWidget(self.journal_search, 1)

        self.journal_who = QComboBox(self)
        self.journal_who.addItem("Все менеджеры", "")
        self.journal_entity = QComboBox(self)
        for title, code in admin.ENTITY_FILTER:
            self.journal_entity.addItem(title, code)
        self.journal_action = QComboBox(self)
        for title, code in admin.ACTION_FILTER:
            self.journal_action.addItem(title, code)
        for combo in (self.journal_who, self.journal_entity, self.journal_action):
            combo.currentIndexChanged.connect(lambda _: self.reload_journal())
            top.addWidget(combo)
        box.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(Metrics.GAP // 2)
        self.journal_period = QComboBox(self)
        for title, days in JOURNAL_PERIODS:
            self.journal_period.addItem(title, days)
        self.journal_period.currentIndexChanged.connect(lambda _: self.reload_journal())
        bottom.addWidget(self.journal_period)

        self.journal_limit = QComboBox(self)
        for count in JOURNAL_LIMITS:
            self.journal_limit.addItem(f"Показать {count}", count)
        self.journal_limit.currentIndexChanged.connect(lambda _: self.reload_journal())
        bottom.addWidget(self.journal_limit)

        self.journal_flagged = QCheckBox("Только требующие проверки", self)
        self.journal_flagged.setToolTip(
            "Удаление оплаты, правка суммы на 20 % и больше, оплата получателю без "
            "карточки поставщика, оплата, заведённая в выходной")
        self.journal_flagged.toggled.connect(lambda _: self._render_journal())
        bottom.addWidget(self.journal_flagged)

        self.journal_count = Hint("", self)
        bottom.addWidget(self.journal_count, 1)

        self.journal_export = QPushButton("Выгрузить в Excel", self)
        self.journal_export.setIcon(icons.icon("export"))
        self.journal_export.clicked.connect(self.export_journal)
        bottom.addWidget(self.journal_export)
        box.addLayout(bottom)
        return box

    def _maintenance_tab(self) -> QWidget:
        """Удаление данных общей базы.

        Отдельной вкладкой, а не кнопкой рядом с правкой учёток: это действие
        выполняют раз в год, и стоять оно должно там, где его не нажмут заодно.
        """
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, Metrics.GAP, 0, 0)
        layout.setSpacing(Metrics.GAP)

        card = Card(page)
        card.body().addWidget(SectionTitle("Удаление данных"))
        card.body().addWidget(Hint(
            "Стирает все оплаты вместе с вложениями, бюджетами, журналом "
            "импортов и привязками получателей — у всех менеджеров сразу. "
            "Нужно, когда выгрузку из 1С переносят заново и накопленное с ней "
            "уже не сходится."))
        card.body().addWidget(Hint(
            "Учётные записи, направления, закрепление поставщиков и журнал "
            "изменений остаются: восстанавливать их руками дольше, чем "
            "повторить импорт. Отменить удаление нельзя — данные вернутся "
            "только из ночной резервной копии сервера."))

        self.scope_label = QLabel("", page)
        self.scope_label.setObjectName("Hint")
        self.scope_label.setWordWrap(True)
        card.body().addWidget(self.scope_label)

        actions = QHBoxLayout()
        self.wipe_button = QPushButton("Удалить данные базы", page)
        self.wipe_button.setObjectName("Danger")
        self.wipe_button.setIcon(icons.icon("trash", Palette.DANGER))
        self.wipe_button.setToolTip(
            "Потребует набрать слово подтверждения и ваш пароль")
        self.wipe_button.clicked.connect(self.wipe_database)
        actions.addWidget(self.wipe_button)
        actions.addStretch(1)
        card.body().addLayout(actions)

        layout.addWidget(card)
        layout.addStretch(1)
        return page

    # --- загрузка -------------------------------------------------------------

    def restore(self) -> None:
        if self._loaded:
            return
        self.reload()

    def reload(self) -> None:
        available = transport.session.active and transport.session.is_admin
        self.offline.setVisible(not available)
        self.tabs.setVisible(available)
        for button in (self.new_button, self.refresh_button,
                       self.edit_button, self.reset_button, self.dds_add_button,
                       self.dds_mark_button,
                       self.fix_button, self.reject_button,
                       self.wipe_button):
            button.setEnabled(available)
        if not available:
            self.subtitle.setText("Требуется вход администратором")
            return

        self._journal_request += 1
        run_task(_load_all, self._journal_query(), on_result=self._apply,
                 on_error=self._failed)

    def _apply(self, payload: tuple) -> None:
        (self.accounts, self.entries, self._known,
         self._directions, self._claims, self._scope, self._dds_items) = payload
        self._loaded = True
        self.accounts_table.set_items(self.accounts)
        self._fill_journal_people()
        self._render_journal()
        self.claims_table.set_items(self._claims)
        self.dds_table.set_items(self._dds_items)
        self._filter_dds(self.dds_search.text())

        people = {p.full_name for entry in self._claims
                  for p in entry.assigned if not p.fixed}
        self.claims_hint.setText(
            f"заявок: {len(self._claims)} от {len(people)} менеджеров"
            if self._claims else "заявок на подтверждение нет")
        self.tabs.setTabText(1, f"Закрепление поставщиков ({len(self._claims)})"
                             if self._claims else "Закрепление поставщиков")
        active = sum(1 for a in self.accounts if a.is_active)
        admins = sum(1 for a in self.accounts if a.is_admin and a.is_active)
        self.subtitle.setText(
            f"Учётных записей: {len(self.accounts)} · с доступом: {active} · "
            f"администраторов: {admins}")
        # Сколько человек из выгрузки остались без учётки: их оплаты видны, но
        # править их некому, и это стоит замечать до того, как спросят.
        linked = {name for account in self.accounts for name in account.responsible}
        self.hint.setText(f"без учётной записи в 1С: {len(set(self._known) - linked)}")
        summary, _behind = admin.update_summary(self.accounts, __version__)
        self.versions.setText(
            summary + ". Прочерк в колонке «Версия» — приложение старше 3.9.2, "
            "оно себя не называет.")

        if not self._scope.supported:
            # Раздел открылся, а удалять нечем: сервер старой версии такого
            # адреса не знает. Молчать об этом нельзя — при полной базе
            # «данных нет» выглядело бы как потеря данных.
            self.scope_label.setText(
                "Серверная часть не обновлена — удаление данных ей ещё "
                "неизвестно. Обновите сервер, и раздел заработает.")
        else:
            self.scope_label.setText(
                f"Сейчас в базе: {self._scope.summary}"
                if not self._scope.empty else "Данных в базе нет — стирать нечего")
        self.wipe_button.setEnabled(self._scope.supported and not self._scope.empty)

    # --- журнал ---------------------------------------------------------------

    def _fill_journal_people(self) -> None:
        """Список менеджеров для отбора — из учёток. Выбранный остаётся выбранным."""
        chosen = self.journal_who.currentData()
        self.journal_who.blockSignals(True)
        self.journal_who.clear()
        self.journal_who.addItem("Все менеджеры", "")
        for account in sorted(self.accounts, key=lambda a: a.title.lower()):
            self.journal_who.addItem(account.title, account.login)
        index = self.journal_who.findData(chosen)
        self.journal_who.setCurrentIndex(max(index, 0))
        self.journal_who.blockSignals(False)

    def _journal_query(self) -> dict:
        """Условия отбора журнала — как их понимает сервер."""
        days = self.journal_period.currentData()
        return {
            "limit": self.journal_limit.currentData() or JOURNAL_LIMITS[0],
            "entity": self.journal_entity.currentData() or "",
            "login": self.journal_who.currentData() or "",
            "action": self.journal_action.currentData() or "",
            "text": self.journal_search.text(),
            # 0 дней — «сегодня»; None — без ограничения периода.
            "start": date.today() - timedelta(days=days) if days is not None else None,
        }

    def reload_journal(self) -> None:
        """Перечитывает только журнал — после смены отбора остальное не трогаем."""
        if not (transport.session.active and transport.session.is_admin):
            return
        self._journal_timer.stop()
        self._journal_request += 1
        request = self._journal_request
        self.journal_count.setText("Загружаю…")
        run_task(_read_journal, self._journal_query(),
                 on_result=lambda entries: self._journal_loaded(request, entries),
                 on_error=self._failed)

    def _journal_loaded(self, request: int, entries: list[Entry]) -> None:
        if request != self._journal_request:
            return
        self.entries = entries
        self._render_journal()

    def _render_journal(self) -> None:
        flagged = sum(1 for entry in self.entries if entry.flags)
        shown = ([e for e in self.entries if e.flags]
                 if self.journal_flagged.isChecked() else self.entries)
        self._journal_shown = shown
        self.journal_table.set_items(shown)
        self.journal_count.setText(
            f"Действий: {len(shown)}"
            + (f" · требуют проверки: {flagged}" if flagged else ""))

    def open_entry(self, entry: Entry) -> None:
        """Двойной щелчок по строке: все подробности действия и история оплаты."""
        JournalEntryDialog(entry, self).exec()

    def export_journal(self) -> None:
        if not self._journal_shown:
            self.notify("В журнале нет строк для выгрузки", ToastKind.WARNING)
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Выгрузить журнал",
            f"Журнал изменений {date.today():%d.%m.%Y}.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        try:
            write_journal(self._journal_shown, path)
        except OSError as error:
            # Чаще всего файл открыт в Excel и занят.
            self.notify(f"Не удалось сохранить файл: {error}", ToastKind.ERROR)
            return
        self.notify(f"Журнал выгружен: {len(self._journal_shown)} строк",
                    ToastKind.SUCCESS)

    def _failed(self, message: str) -> None:
        self.notify(message, ToastKind.ERROR)

    # --- действия -------------------------------------------------------------

    def _chosen(self) -> Account | None:
        rows = self.accounts_table.selected_items()
        return rows[0] if rows else None

    def create_account(self) -> None:
        dialog = AccountDialog(Account(), self._known,
                               directions=self._directions, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        account = dialog.result_account()
        run_task(lambda: admin.create(account),
                 on_result=self._created, on_error=self._failed)

    def _created(self, payload: tuple[Account, str]) -> None:
        account, password = payload
        PasswordShown(account.login, password, self).exec()
        self.notify(f"Учётная запись {account.login} заведена", ToastKind.SUCCESS)
        self.reload()

    def edit_account(self, account: Account | None) -> None:
        if account is None:
            self.notify("Выберите учётную запись", ToastKind.INFO)
            return
        dialog = AccountDialog(account, self._known,
                               is_self=account.login == transport.session.login,
                               directions=self._directions, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        changed = dialog.result_account()
        run_task(lambda: admin.save(changed),
                 on_result=lambda _: self._saved(changed), on_error=self._failed)

    def _saved(self, account: Account) -> None:
        self.notify(f"Учётная запись {account.login} изменена", ToastKind.SUCCESS)
        self.reload()

    def reset_password(self) -> None:
        account = self._chosen()
        if account is None:
            self.notify("Выберите учётную запись", ToastKind.INFO)
            return
        answer = QMessageBox.question(
            self, "Назначить новый пароль",
            f"Назначить новый пароль для {account.title}?\n\n"
            "Прежний перестанет работать сразу. Новый нужно будет передать "
            "лично — при первом входе владелец заменит его на свой.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        run_task(lambda: admin.reset_password(account.id),
                 on_result=lambda password: self._reset_done(account, password),
                 on_error=self._failed)

    def _reset_done(self, account: Account, password: str) -> None:
        PasswordShown(account.login, password, self).exec()
        self.reload()

    # --- статьи ДДС -----------------------------------------------------------

    def _filter_dds(self, text: str) -> None:
        self.dds_table.proxy.set_text(text)
        self.dds_table.model_.set_terms([t for t in text.split() if len(t) > 1])
        shown = self.dds_table.proxy.rowCount()
        total = len(self._dds_items)
        self.dds_hint.setText(
            f"статей: {total}" if shown == total else f"показано {shown} из {total}")

    def add_dds_item(self) -> None:
        title = dds.clean(self.dds_new.text())
        if not title:
            self.notify("Введите название статьи", ToastKind.INFO)
            return
        known = next((item.title for item in self._dds_items
                      if item.title.casefold() == title.casefold()), "")
        if known:
            self.notify(f"Статья «{known}» уже есть в списке", ToastKind.WARNING)
            return
        self.dds_add_button.setEnabled(False)
        run_task(lambda: dds.add(title),
                 on_result=self._dds_added, on_error=self._dds_failed)

    def mark_dds_items(self) -> None:
        chosen = [item for item in self.dds_table.selected_items()
                  if isinstance(item, dds.DdsItem)]
        if not chosen:
            self.notify("Выберите статьи в таблице", ToastKind.INFO)
            return
        if any(not item.id for item in chosen):
            self.notify("Серверная часть не обновлена — цвета ей ещё неизвестны",
                        ToastKind.WARNING)
            return
        dialog = DdsMarkDialog(chosen, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        color, note = dialog.result_mark()
        ids = [item.id for item in chosen]
        run_task(lambda: dds.set_mark(ids, color, note),
                 on_result=lambda count: self._dds_marked(count, color, note),
                 on_error=self._dds_failed)

    def _dds_marked(self, count: int, color: str, note: str) -> None:
        what = "пометка снята" if not (color or note) else "цвет и метка заданы"
        self.notify(f"Статей: {count}, {what}", ToastKind.SUCCESS)
        self.reload()

    def _dds_added(self, title: str) -> None:
        self.dds_new.clear()
        self.dds_add_button.setEnabled(True)
        self.notify(f"Статья «{title}» добавлена", ToastKind.SUCCESS)
        # Поиск сбрасывается: добавленную статью иначе скрыл бы прежний фильтр.
        self.dds_search.clear()
        self.reload()

    def _dds_failed(self, message: str) -> None:
        self.dds_add_button.setEnabled(True)
        self.notify(message, ToastKind.ERROR)

    # --- закрепление поставщиков ----------------------------------------------

    def _chosen_claims(self) -> list[directory.Entry]:
        return [row for row in self.claims_table.selected_items()
                if isinstance(row, directory.Entry)]

    def fix_claims(self) -> None:
        rows = self._chosen_claims()
        if not rows:
            self.notify("Выберите заявки в таблице", ToastKind.INFO)
            return
        answer = QMessageBox.question(
            self, "Зафиксировать закрепление",
            f"Подтвердить заявки по {len(rows)} поставщикам?\n\n"
            "После фиксации менеджер не сможет снять закрепление сам.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            return
        keys = [row.recipient_key for row in rows]
        run_task(lambda: directory.fix(keys),
                 on_result=lambda result: self._claims_done(
                     f"Зафиксировано закреплений: {result.changed}"),
                 on_error=self._failed)

    def reject_claims(self) -> None:
        rows = self._chosen_claims()
        if not rows:
            self.notify("Выберите заявки в таблице", ToastKind.INFO)
            return
        # Снимаются только незафиксированные: отклонение заявки и снятие
        # действующего закрепления — разные решения, и путать их в одной
        # кнопке нельзя.
        drafts = [(row.recipient_key, [p.user_id for p in row.assigned
                                       if not p.fixed]) for row in rows]
        answer = QMessageBox.question(
            self, "Отклонить заявки",
            f"Снять заявки по {len(drafts)} поставщикам?\n\n"
            "Менеджер сможет подать их снова.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        run_task(lambda: _reject(drafts),
                 on_result=lambda removed: self._claims_done(
                     f"Заявок отклонено: {removed}"),
                 on_error=self._failed)

    def _claims_done(self, message: str) -> None:
        self.notify(message, ToastKind.SUCCESS)
        self.reload()

    # --- удаление данных ------------------------------------------------------

    def wipe_database(self) -> None:
        """Удаление данных общей базы после двойного подтверждения."""
        if self._scope.empty:
            self.notify("Данных в базе нет — стирать нечего", ToastKind.INFO)
            return
        dialog = WipeDialog(self._scope, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        password = dialog.result_password()
        self.wipe_button.setEnabled(False)
        run_task(lambda: admin.wipe(password),
                 on_result=self._wiped, on_error=self._wipe_failed)

    def _wiped(self, erased: Scope) -> None:
        self.notify(f"Данные удалены: {erased.summary}", ToastKind.SUCCESS)
        if self._on_wiped:
            self._on_wiped()
        self.reload()

    def _wipe_failed(self, message: str) -> None:
        # Кнопку возвращаем на место: неверный пароль — это повод повторить, а
        # не причина закрыть раздел до обновления страницы.
        self.wipe_button.setEnabled(True)
        self.notify(message, ToastKind.ERROR)


def _dot(item: dds.DdsItem):
    color = QColor(item.color) if item.color else None
    return marks.dot_icon(color) if color and color.isValid() else None


def _tint(item: dds.DdsItem):
    color = QColor(item.color) if item.color else None
    return marks.tint(color) if color and color.isValid() else None


def _ink(item: dds.DdsItem):
    color = QColor(item.color) if item.color else None
    return marks.ink(color) if color and color.isValid() else None


def _color_name(item: dds.DdsItem) -> str:
    return next((name for name, code in dds.COLORS if code.upper() == item.color.upper()),
                item.color)


def _load_all(journal_query: dict) -> tuple:
    """Учётки, журнал, имена из 1С, направления, заявки и объём базы.

    Одним походом: страница показывает всё сразу, а шесть последовательных
    запросов из интерфейса — это шесть поводов подождать.
    """
    known = data.known_values().get("responsible", [])
    # В учётку — только отделы: расход («Налоги») человеку не назначают.
    directions = [(item.code, item.title) for item in directory.directions()
                  if item.for_people]
    claims = directory.pending_claims().items
    return (admin.accounts(), admin.journal(**journal_query), known, directions, claims,
            admin.scope(), dds.catalog())


def _reject(drafts: list[tuple[str, list[int]]]) -> int:
    """Снимает незафиксированные заявки, каждую у своих менеджеров.

    По одному поставщику за раз, а не общим списком: у разных поставщиков
    заявители разные, и снять «всех сразу» значило бы задеть чужие заявки на
    соседних строках.
    """
    removed = 0
    for key, user_ids in drafts:
        if user_ids:
            removed += directory.unassign([key], user_ids).changed
    return removed
