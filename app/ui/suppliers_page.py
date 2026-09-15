"""Вкладка «Поставщики»: список с направлениями, закреплением и карточкой.

У каждого поставщика свой файл переоценки и своя структура данных. База помнит
разбор каждого — как читать его прайс и какие позиции пришлось свести вручную, —
поэтому второй прайс того же поставщика обрабатывается почти без ручной работы.

Список строится из оплат, а не из карточек. Карточку заводят руками и только
когда нужен разбор прайса, поэтому их единицы; получатель же приходит с каждой
выгрузкой 1С, и таких — почти шестьсот. Список из карточек показывал бы пустую
вкладку при полутора тысячах реальных поставщиков.

Кто ведёт поставщика и кто ему платил — разные вопросы, и здесь они разведены.
Раньше менеджер вычислялся по оплатам, и разовый платёж за коллегу навсегда
записывал человека в ведущие. Теперь закрепление заявляет сам менеджер, а
подтверждает администратор; заявленное считается своим сразу, не дожидаясь
подтверждения, — иначе между заявкой и фиксацией работать было бы нечем.

Направление отбирает список, но ничего не прячет: чаще всего вкладку открывают
с вопросом «не помню, чей это поставщик», и жёсткое ограничение выдачи убило бы
главный сценарий. Отбор и сортировку считает сервер — на полутора тысячах строк
пересобирать список на клиенте при каждой смене фильтра уже нельзя.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import suppliers
from ..core.payments import transport
from ..core.suppliers import Supplier, SupplierLayout, SupplierLink, directory
from ..core.normalize import normalize_text
from ..core.settings import AppSettings
from . import icons
from .tasks import run_task
from .theme import Metrics, Palette
from .widgets.common import (
    Badge,
    Card,
    Divider,
    Hint,
    MetricTile,
    SectionTitle,
    Subtitle,
    Title,
    fade_in,
)
from .widgets.inputs import SelectBox
from .widgets.supplier_dialogs import SupplierLayoutDialog
from .widgets.table import Column, DataTable
from .widgets.toast import ToastKind

PAGE_SIZE = 100

# Колонки, которые при первом клике сортируются по возрастанию.
_TEXT_SORTS = frozenset({"name", "manager", "direction"})

# Отбор по состоянию закрепления. «Без закрепления» — рабочий список на время
# разметки, а не изъян: пока поставщика никто не заявил, он именно такой.
_STATES = (
    ("", "Любое закрепление"),
    ("fixed", "Зафиксированы"),
    ("draft", "На подтверждении"),
    ("none", "Без закрепления"),
)


@dataclass(slots=True)
class Row:
    """Строка таблицы: поставщик из общей базы и его карточка, если заведена."""

    entry: directory.Entry
    card: Supplier | None = None

    @property
    def name(self) -> str:
        return self.entry.recipient

    @property
    def key(self) -> str:
        return self.entry.recipient_key

    @property
    def directions(self) -> str:
        """Направление. Второе — плюсом: поставщик в обоих случай редкий."""
        titles = self.entry.directions
        if not titles:
            return ""
        first = _DIRECTION_TITLES.get(titles[0], titles[0])
        return first if len(titles) == 1 else f"{first} +{len(titles) - 1}"

    @property
    def assigned(self) -> str:
        return self.entry.assigned_title or "—"

    @property
    def card_state(self) -> str:
        if self.card is None:
            # Отсутствие карточки — не изъян: она нужна только для разбора
            # прайса. Но знать об этом, глядя на список, полезно.
            return ""
        return f"структур: {self.card.layouts}" if self.card.layouts else "заведена"

    @property
    def amount_title(self) -> str:
        return f"{self.entry.amount:,.0f}".replace(",", " ") if self.entry.amount else ""

    @property
    def last_pay_title(self) -> str:
        return f"{self.entry.last_pay:%d.%m.%Y}" if self.entry.last_pay else ""


# Заголовки направлений по коду. Заполняется при первой загрузке справочника:
# в таблице нужен «Beauty», а не «beauty», и тянуть справочник в каждую строку
# ради одного слова незачем.
_DIRECTION_TITLES: dict[str, str] = {}


def _load_page(search: str, direction: str, manager: int, state: str,
               sort: str, order: str, page: int) -> tuple[directory.Page,
                                                          list[Supplier]]:
    """Страница списка и карточки — одним походом, в фоновой задаче."""
    cards = suppliers.list_suppliers()
    if not directory.online():
        # Без входа в общую базу закреплений нет вовсе. Показываем карточки:
        # разбор прайсов лежит в них и работает офлайн.
        return _from_cards(cards, search), cards
    try:
        page_data = directory.suppliers(
            search=search, direction=direction, manager=manager, state=state,
            sort=sort, order=order, page=page, page_size=PAGE_SIZE)
    except Exception:  # noqa: BLE001 — сервер не должен ронять вкладку
        return _from_cards(cards, search), cards
    return page_data, cards


def _from_cards(cards: list[Supplier], search: str) -> directory.Page:
    """Список из одних карточек — то, что можно показать без общей базы."""
    query = search.casefold().replace("ё", "е").strip()
    items = [directory.Entry(recipient_key=card.key, recipient=card.name)
             for card in cards
             if not query or query in card.name.casefold().replace("ё", "е")]
    return directory.Page(items=items, total=len(items), page=1,
                          page_size=max(len(items), 1))


class SuppliersPage(QWidget):
    """Список поставщиков слева, карточка выбранного справа."""

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, ToastKind], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self.items: list[Supplier] = []
        self.rows: list[Row] = []
        self.current: Supplier | None = None
        self.chosen: Row | None = None
        self._layouts: list[SupplierLayout] = []
        self._links: list[SupplierLink] = []
        self._loading = False
        self._page = directory.Page()
        # Состояние отбора. Держится здесь, а не считывается с виджетов:
        # запрос уходит на сервер, и собирать его из полудюжины полей на каждом
        # обращении — верный способ разойтись с тем, что видит человек.
        self._direction = ""
        self._manager = 0
        # Чьи справочники сейчас в фильтрах. Страница строится при запуске
        # приложения, а вход в общую базу человек выполняет позже, открыв
        # «Оплаты», — на момент сборки направлений и менеджеров ещё не
        # существует. Сравнение с учётной записью заодно переставляет фильтры,
        # если вошли под другой.
        self._filters_for = -1
        self._state = ""
        self._sort = "amount"
        self._order = "desc"
        self._page_no = 1

        # Поиск не дёргает сервер на каждую букву: запрос уходит, когда человек
        # остановился. Полторы тысячи строк искать посимвольно незачем.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(350)
        self._search_timer.timeout.connect(self._search_changed)

        self._build()
        self.reload()

    # --- интерфейс ------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Title("Поставщики", self))
        root.addWidget(Subtitle(
            "Поставщик закрепляется за менеджером, а не вычисляется по оплатам. "
            "Отметьте своих и отправьте на подтверждение — до него они уже "
            "считаются вашими. База помнит структуру прайса каждого поставщика "
            "и позиции, которые пришлось свести вручную.", self))

        root.addWidget(self._metrics_row())
        root.addWidget(self._workspace(), 1)

    def _metrics_row(self) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Metrics.GAP)
        self.tile_suppliers = MetricTile("Поставщиков", Palette.PRIMARY, row)
        self.tile_mine = MetricTile("Закреплено за мной", Palette.SUCCESS, row)
        self.tile_layouts = MetricTile("Структур прайсов", Palette.INFO, row)
        self.tile_links = MetricTile("Ручных привязок", Palette.TEXT_MUTED, row)
        for tile in (self.tile_suppliers, self.tile_mine,
                     self.tile_layouts, self.tile_links):
            layout.addWidget(tile)
        return row

    def _workspace(self) -> QSplitter:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._list_card())
        splitter.addWidget(self._detail_card())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 520])
        return splitter

    def _list_card(self) -> Card:
        card = Card(self, padding=Metrics.PAD - 4)
        body = card.body()
        body.setSpacing(Metrics.GAP - 4)
        body.addWidget(SectionTitle("Список", card))
        body.addLayout(self._filter_row(card))
        body.addWidget(self._table(card), 1)
        body.addLayout(self._pager(card))
        body.addLayout(self._actions(card))
        return card

    def _filter_row(self, card: Card) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(6)

        # Направление — переключателем, а не списком: его меняют чаще всего
        # остального, и лишний клик по выпадающему списку тут заметен.
        self.direction_row = QHBoxLayout()
        self.direction_row.setSpacing(4)
        self.direction_group = QButtonGroup(card)
        self.direction_group.setExclusive(True)
        self.direction_group.idClicked.connect(self._direction_chosen)
        self._add_direction_button(card, "Все", "", 0)
        self.direction_row.addStretch(1)
        box.addLayout(self.direction_row)

        line = QHBoxLayout()
        line.setSpacing(7)

        self.search = QLineEdit(card)
        self.search.setPlaceholderText("Поиск по названию…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _: self._search_timer.start())
        self.search.returnPressed.connect(self._search_changed)
        line.addWidget(self.search, 2)

        self.manager = SelectBox(card)
        self.manager.setToolTip(
            "«Мои» — и зафиксированные, и заявленные: поставщик считается вашим "
            "сразу после заявки")
        self.manager.currentIndexChanged.connect(lambda _: self._manager_chosen())
        line.addWidget(self.manager, 1)

        self.state = SelectBox(card)
        for value, title in _STATES:
            self.state.addItem(title, value)
        self.state.currentIndexChanged.connect(lambda _: self._state_chosen())
        line.addWidget(self.state, 1)
        box.addLayout(line)
        return box

    def _add_direction_button(self, parent: QWidget, title: str, code: str,
                              index: int) -> None:
        button = QPushButton(title, parent)
        button.setCheckable(True)
        button.setChecked(not code)
        button.setProperty("code", code)
        self.direction_group.addButton(button, index)
        self.direction_row.insertWidget(index, button)

    def _table(self, card: Card) -> DataTable:
        self.table = DataTable([
            Column("Поставщик", lambda r: r.name, 260, highlight=True),
            Column("Направление", lambda r: r.directions, 130,
                   color=lambda r: None if r.entry.directions
                   else QColor(Palette.TEXT_FAINT)),
            Column("Ведёт", lambda r: r.assigned, 200,
                   color=lambda r: QColor(Palette.WARNING) if r.entry.has_draft
                   else None),
            Column("Оплат", lambda r: r.entry.payments or "", 70,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            Column("Сумма", lambda r: r.amount_title, 110,
                   align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            Column("Последняя оплата", lambda r: r.last_pay_title, 130),
            Column("Карточка", lambda r: r.card_state, 110),
        ], card)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        # Сортировку считает сервер: страница показывает сотню строк из полутора
        # тысяч, и сортировка внутри неё переставляла бы видимое, оставляя
        # остальное на месте — то есть врала бы.
        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().sectionClicked.connect(self._sort_by)
        selection = self.table.selectionModel()
        selection.currentRowChanged.connect(lambda *_: self._on_selected())
        # Отдельно от смены текущей строки: при `selectRow` Qt сдвигает курсор
        # раньше, чем помечает строки выделенными, и состояние кнопок,
        # посчитанное по первому сигналу, отстаёт от того, что видно на экране.
        selection.selectionChanged.connect(lambda *_: self._update_actions())
        self.table.item_activated.connect(lambda _: self.tabs.setCurrentIndex(0))
        return self.table

    def _pager(self, card: Card) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)
        self.page_hint = Hint("", card)
        row.addWidget(self.page_hint)
        row.addStretch(1)
        self.prev_button = QPushButton("‹ Назад", card)
        self.prev_button.clicked.connect(lambda: self._turn(-1))
        row.addWidget(self.prev_button)
        self.next_button = QPushButton("Вперёд ›", card)
        self.next_button.clicked.connect(lambda: self._turn(1))
        row.addWidget(self.next_button)
        return row

    def _actions(self, card: Card) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)

        self.claim_button = QPushButton("Это мои", card)
        self.claim_button.setObjectName("Primary")
        self.claim_button.setIcon(icons.icon("check", Palette.TEXT_ON_PRIMARY))
        self.claim_button.setToolTip(
            "Заявить выделенных поставщиков своими.\n"
            "Они сразу попадут в отбор «мои» — с пометкой «на подтверждении».")
        self.claim_button.clicked.connect(self.claim_selected)
        row.addWidget(self.claim_button)

        self.withdraw_button = QPushButton("Отозвать", card)
        self.withdraw_button.setToolTip(
            "Снять свою заявку. Зафиксированное так не снимается — это к админу")
        self.withdraw_button.clicked.connect(self.withdraw_selected)
        row.addWidget(self.withdraw_button)

        self.fix_button = QPushButton("Зафиксировать", card)
        self.fix_button.setIcon(icons.icon("admin"))
        self.fix_button.setToolTip("Подтвердить заявки выделенных поставщиков")
        self.fix_button.clicked.connect(self.fix_selected)
        row.addWidget(self.fix_button)
        row.addStretch(1)

        add = QPushButton("Карточка", card)
        add.setIcon(icons.icon("card"))
        add.setToolTip("Завести карточку выбранного поставщика — для разбора прайса")
        add.clicked.connect(self.add_supplier)
        row.addWidget(add)

        remove = QPushButton("Удалить", card)
        remove.setObjectName("Danger")
        remove.setIcon(icons.icon("trash", Palette.DANGER))
        remove.setToolTip("Удалить карточку вместе со структурами и привязками")
        remove.clicked.connect(self.delete_supplier)
        row.addWidget(remove)
        return row

    def _detail_card(self) -> Card:
        card = Card(self, padding=Metrics.PAD - 4)
        body = card.body()
        body.setSpacing(Metrics.GAP - 4)

        header = QHBoxLayout()
        header.setSpacing(9)
        self.detail_title = SectionTitle("Карточка поставщика", card)
        header.addWidget(self.detail_title)
        header.addStretch(1)
        self.save_button = QPushButton("Сохранить", card)
        self.save_button.setObjectName("Primary")
        self.save_button.setIcon(icons.icon("save", Palette.TEXT_ON_PRIMARY))
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_current)
        header.addWidget(self.save_button)
        body.addLayout(header)

        badges = QHBoxLayout()
        badges.setSpacing(6)
        self.direction_badge = Badge("", Palette.PRIMARY, Palette.PRIMARY_SOFT, card)
        self.direction_badge.hide()
        badges.addWidget(self.direction_badge)
        self.assigned_label = Hint("", card)
        badges.addWidget(self.assigned_label, 1)
        body.addLayout(badges)

        self.warning_label = Hint("", card)
        self.warning_label.setStyleSheet(f"color: {Palette.WARNING}; font-size: 12px;")
        self.warning_label.hide()
        body.addWidget(self.warning_label)

        self.tabs = QTabWidget(card)
        self.tabs.addTab(self._card_tab(), "Карточка")
        self.tabs.addTab(self._layouts_tab(), "Структуры прайсов")
        self.tabs.addTab(self._links_tab(), "Ручные привязки")
        body.addWidget(self.tabs, 1)
        return card

    def _card_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, Metrics.GAP, 0, 0)
        layout.setSpacing(6)

        self.field_name = self._field(layout, page, "Название", "Как называть поставщика")
        self.field_aliases = self._field(
            layout, page, "Ещё имена в файлах",
            "Через запятую. По ним поставщик узнаётся, если в имени файла "
            "стоит не его название: «зелински, zr, ИП Саух»")
        self.field_brands = self._field(layout, page, "Бренды", "Через запятую")
        self.field_categories = self._field(layout, page, "Категории", "Через запятую")
        self.field_contact = self._field(layout, page, "Контакт", "Почта, телефон, менеджер")

        layout.addWidget(QLabel("Отсрочка платежа", page))
        terms_row = QHBoxLayout()
        terms_row.setSpacing(8)
        self.field_terms = SelectBox(page)
        self.field_terms.setEditable(True)
        # Ноль — не «без отсрочки», а «взять из истории оплат»: у большинства
        # поставщиков она устойчива, и посчитанная медиана точнее введённой
        # на память. Готовые значения — те, что назвал пользователь.
        self.field_terms.addItem("по истории оплат", 0)
        for days in (7, 14, 30, 60):
            self.field_terms.addItem(f"{days} дней", days)
        self.field_terms.currentTextChanged.connect(lambda _: self._touch())
        terms_row.addWidget(self.field_terms)
        self.terms_hint = Hint("", page)
        terms_row.addWidget(self.terms_hint, 1)
        layout.addLayout(terms_row)

        layout.addWidget(QLabel("Заметка", page))
        self.field_note = QPlainTextEdit(page)
        self.field_note.setPlaceholderText("Периодичность переоценки, особенности прайса…")
        self.field_note.setMinimumHeight(70)
        self.field_note.textChanged.connect(self._touch)
        layout.addWidget(self.field_note, 1)

        layout.addWidget(Divider(page))
        self.card_hint = Hint("", page)
        layout.addWidget(self.card_hint)
        return page

    def _fill_terms(self, supplier: Supplier | None) -> None:
        """Показывает заданную отсрочку и рядом — посчитанную по истории оплат."""
        days = supplier.payment_terms_days if supplier else 0
        self.field_terms.blockSignals(True)
        index = self.field_terms.findData(days)
        if index >= 0:
            self.field_terms.setCurrentIndex(index)
        else:
            self.field_terms.setCurrentText(f"{days} дней")
        self.field_terms.blockSignals(False)
        self.terms_hint.setText("")
        if supplier is None or not supplier.id:
            return
        run_task(
            _history_terms, supplier.id, supplier.name,
            on_result=lambda value: self.terms_hint.setText(
                f"по истории оплат: {value:.0f} дн" if value
                else "истории оплат для расчёта пока мало"),
            on_error=lambda _: self.terms_hint.setText(""))

    def _field(self, layout: QVBoxLayout, parent: QWidget, label: str, hint: str) -> QLineEdit:
        layout.addWidget(QLabel(label, parent))
        field = QLineEdit(parent)
        field.setPlaceholderText(hint)
        field.textChanged.connect(self._touch)
        layout.addWidget(field)
        return field

    def _layouts_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, Metrics.GAP, 0, 0)
        layout.setSpacing(Metrics.GAP - 4)
        layout.addWidget(Hint(
            "Структура запоминается при каждом сравнении: какой лист читать и "
            "какая колонка прайса заполняет какой вид цены 1С. Поставщик может "
            "присылать файлы в нескольких форматах — тогда структур несколько.",
            page))

        self.layouts_list = QListWidget(page)
        self.layouts_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.layouts_list.setToolTip("Двойной клик — поправить разбор")
        self.layouts_list.itemDoubleClicked.connect(lambda _: self.edit_layout())
        layout.addWidget(self.layouts_list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(7)
        add = QPushButton("Добавить прайс", page)
        add.setObjectName("Primary")
        add.setIcon(icons.icon("open", Palette.TEXT_ON_PRIMARY))
        add.setToolTip(
            "Разобрать файл поставщика и запомнить его структуру.\n"
            "Нужно, когда поставщик прислал файл нового формата: без этого\n"
            "приложение не узнает его и заведёт вторую карточку.")
        add.clicked.connect(self.add_layout)
        buttons.addWidget(add)

        edit = QPushButton("Изменить", page)
        edit.setIcon(icons.icon("columns"))
        edit.setToolTip("Поправить роли колонок и соответствие видов цен")
        edit.clicked.connect(self.edit_layout)
        buttons.addWidget(edit)

        remove = QPushButton("Забыть", page)
        remove.setObjectName("Danger")
        remove.setIcon(icons.icon("trash", Palette.DANGER))
        remove.setToolTip("Следующий прайс такого вида будет разобран заново")
        remove.clicked.connect(self.delete_layout)
        buttons.addWidget(remove)
        layout.addLayout(buttons)
        return page

    def _links_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, Metrics.GAP, 0, 0)
        layout.setSpacing(Metrics.GAP - 4)
        layout.addWidget(Hint(
            "Позиции, которые приложение не свело само, а вы указали вручную. "
            "Они применяются раньше автоматического подбора. Если поставщик "
            "отдал артикул другому товару — привязку нужно снять.", page))

        self.links_table = DataTable([
            Column("Товар 1С", lambda l: l.onec_name, 300, highlight=True),
            Column("Артикул 1С", lambda l: l.onec_article, 160, highlight=True),
            Column("Товар поставщика", lambda l: l.supplier_name, 280, highlight=True),
            Column("Артикул поставщика", lambda l: l.supplier_article, 160, highlight=True),
            Column("Кто", lambda l: l.author, 110),
            Column("Когда", lambda l: l.created_at.strftime("%d.%m.%Y") if l.created_at else "",
                   100, sort_key=lambda l: l.created_at or 0),
        ], page)
        self.links_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        layout.addWidget(self.links_table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(7)
        remove = QPushButton("Снять привязку", page)
        remove.setObjectName("Danger")
        remove.setIcon(icons.icon("unlink", Palette.DANGER))
        remove.clicked.connect(self.delete_link)
        buttons.addWidget(remove)

        clear = QPushButton("Снять все", page)
        clear.setObjectName("Danger")
        clear.clicked.connect(self.clear_links)
        buttons.addWidget(clear)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return page

    # --- отбор ----------------------------------------------------------------

    def _load_directions(self) -> None:
        """Справочник направлений и менеджеры — после входа в общую базу."""
        run_task(
            lambda: (directory.directions(), directory.managers()),
            on_result=self._fill_filters,
            on_error=self._filters_failed)

    def _filters_failed(self, message: str) -> None:
        """Справочники не пришли — список всё равно нужно показать.

        Отметка о загрузке ставится и здесь: иначе `reload` уходил бы за
        справочниками снова и снова, и список не прочитался бы ни разу.
        """
        self._filters_for = transport.session.user_id
        self.notify(f"Направления недоступны: {message}", ToastKind.WARNING)
        self.reload()

    def _fill_filters(self, payload: tuple[list[directory.Direction],
                                           list[tuple[int, str]]]) -> None:
        directions, managers = payload
        self._filters_for = transport.session.user_id
        _DIRECTION_TITLES.update({d.code: d.title for d in directions})

        # Кнопки пересобираются целиком: вход мог смениться, а вместе с ним и
        # справочник. Оставленные от прошлого раза дали бы два «Beauty» подряд.
        for button in self.direction_group.buttons():
            if button.property("code"):
                self.direction_group.removeButton(button)
                button.setParent(None)
                button.deleteLater()
        for index, item in enumerate(directions, start=1):
            self._add_direction_button(self, item.title, item.code, index)

        self.manager.blockSignals(True)
        self.manager.clear()
        mine = transport.session.user_id
        if mine:
            self.manager.addItem("Мои поставщики", mine)
        self.manager.addItem("Все менеджеры", 0)
        for user_id, name in managers:
            if user_id != mine:
                self.manager.addItem(name, user_id)
        self.manager.blockSignals(False)

        # Свои поставщики нужнее чужих, а своё направление — чужого. И то, и
        # другое лишь умолчание отбора: переключатель рядом, и чужое доступно.
        self._manager = mine
        if own := _own_direction(directions):
            self._select_direction(own)
        self.reload()

    def _select_direction(self, code: str) -> None:
        for button in self.direction_group.buttons():
            if button.property("code") == code:
                button.setChecked(True)
                self._direction = code
                return

    def _direction_chosen(self, index: int) -> None:
        button = self.direction_group.button(index)
        self._direction = button.property("code") if button else ""
        self._page_no = 1
        self.reload()

    def _manager_chosen(self) -> None:
        self._manager = int(self.manager.currentData() or 0)
        self._page_no = 1
        self.reload()

    def _state_chosen(self) -> None:
        self._state = self.state.currentData() or ""
        self._page_no = 1
        self.reload()

    def _search_changed(self) -> None:
        self._search_timer.stop()
        self._page_no = 1
        self.reload()

    def _sort_by(self, column: int) -> None:
        """Клик по заголовку. Повторный по той же колонке меняет направление.

        Сортировка по менеджеру и направлению идёт по первому значению: у
        поставщика их бывает несколько, и незакреплённые всегда уходят в
        конец — список открывают ради того, кто ведёт.
        """
        keys = {0: "name", 1: "direction", 2: "manager",
                3: "payments", 4: "amount", 5: "last_pay"}
        key = keys.get(column)
        if key is None:
            return
        if key == self._sort:
            self._order = "asc" if self._order == "desc" else "desc"
        else:
            # Имена — от «А», деньги и даты — от большего: так их и читают.
            self._sort = key
            self._order = "asc" if key in _TEXT_SORTS else "desc"
        self._page_no = 1
        self.reload()

    def _turn(self, step: int) -> None:
        self._page_no = max(1, min(self._page.pages, self._page_no + step))
        self.reload()

    # --- данные ---------------------------------------------------------------

    def reload(self) -> None:
        """Читает страницу списка. Вызывается при каждой смене отбора.

        Справочники подтягиваются здесь же, когда появляется вход: собрать их
        в конструкторе нельзя — окно строится до того, как человек вошёл в
        общую базу, и направлений с менеджерами тогда ещё не существует.
        """
        if directory.online() and self._filters_for != transport.session.user_id:
            # Загрузка справочников закончится вызовом reload — список
            # прочитается следом, уже с правильным отбором по умолчанию.
            self._load_directions()
            return

        run_task(
            lambda: _load_page(self.search.text(), self._direction, self._manager,
                               self._state, self._sort, self._order, self._page_no),
            on_result=self._on_loaded,
            on_error=lambda message: self.notify(
                f"Список поставщиков недоступен: {message}", ToastKind.ERROR),
        )

    def _on_loaded(self, payload: tuple[directory.Page, list[Supplier]]) -> None:
        page, cards = payload
        self.items = cards
        self._page = page
        by_key = {card.key: card for card in cards}
        self.rows = [Row(entry=entry, card=by_key.get(normalize_text(entry.recipient)))
                     for entry in page.items]

        previous = self.chosen.key if self.chosen else ""
        self._fill_table(select=previous)
        self._fill_metrics(page, cards)
        self._fill_pager(page)
        self._update_actions()
        fade_in(self.table)

    def _fill_metrics(self, page: directory.Page, cards: list[Supplier]) -> None:
        self.tile_suppliers.set_value(page.total)
        mine = transport.session.user_id
        self.tile_mine.set_value(
            sum(1 for row in self.rows if mine and row.entry.mine(mine)))
        self.tile_layouts.set_value(sum(s.layouts for s in cards))
        self.tile_links.set_value(sum(s.links for s in cards))

    def _fill_pager(self, page: directory.Page) -> None:
        if not page.total:
            self.page_hint.setText("ничего не найдено")
        else:
            first = (page.page - 1) * page.page_size + 1
            last = min(page.total, first + len(page.items) - 1)
            self.page_hint.setText(f"{first}–{last} из {page.total}")
        self.prev_button.setEnabled(page.page > 1)
        self.next_button.setEnabled(page.page < page.pages)

    def _fill_table(self, select: str = "") -> None:
        self._loading = True
        self.table.set_items(self.rows)
        self.table.proxy.set_text("")
        self.table.model_.set_terms([self.search.text().strip()])
        self._loading = False
        if not self.rows:
            self.chosen = None
            self._show(None)
            self._show_directory(None)
            return
        position = next((i for i, row in enumerate(self.rows) if row.key == select), 0)
        self.table.selectRow(position)

    def _on_selected(self) -> None:
        if self._loading:
            return
        row = self.table.current_item()
        self.chosen = row if isinstance(row, Row) else None
        self._show(self.chosen.card if self.chosen else None)
        self._show_directory(self.chosen)
        self._update_actions()
        if self.chosen is not None and self.chosen.card is None:
            # Карточки нет — показать нечего, но человек должен видеть, что
            # выбрал, и понимать, чего именно не хватает.
            self.detail_title.setText(self.chosen.name)
            self.field_name.setText(self.chosen.name)

    def _show_directory(self, row: Row | None) -> None:
        """Направление, закрепление и предупреждение об оплатах со стороны."""
        if row is None:
            self.direction_badge.hide()
            self.assigned_label.setText("")
            self.warning_label.hide()
            return

        titles = [_DIRECTION_TITLES.get(code, code) for code in row.entry.directions]
        self.direction_badge.setText(" · ".join(titles))
        self.direction_badge.setVisible(bool(titles))

        if row.entry.assigned:
            self.assigned_label.setText(f"ведёт: {row.entry.assigned_title}")
        else:
            self.assigned_label.setText("не закреплён ни за кем")

        payers = row.entry.unassigned_payers
        # Оплата за коллегу ничего не нарушает и ничего не блокирует. Но знать
        # о ней полезно: чаще всего это признак незаявленного закрепления.
        self.warning_label.setText(
            f"платили, но не закреплены: {', '.join(payers)}" if payers else "")
        self.warning_label.setVisible(bool(payers))

    def _update_actions(self) -> None:
        """Что сейчас можно нажать. Офлайн закрепление недоступно вовсе."""
        online = directory.online()
        rows = self._selected_rows()
        mine = transport.session.user_id
        self.claim_button.setEnabled(bool(online and rows and mine))
        self.withdraw_button.setEnabled(bool(
            online and mine and any(
                any(p.user_id == mine and not p.fixed for p in row.entry.assigned)
                for row in rows)))
        self.fix_button.setVisible(transport.session.is_admin)
        self.fix_button.setEnabled(bool(
            online and any(row.entry.has_draft for row in rows)))

    def _selected_rows(self) -> list[Row]:
        return [row for row in self.table.selected_items() if isinstance(row, Row)]

    def _show(self, supplier: Supplier | None) -> None:
        self.current = supplier
        self.save_button.setEnabled(False)
        self._loading = True
        try:
            self.detail_title.setText(supplier.name if supplier else "Карточка поставщика")
            self.field_name.setText(supplier.name if supplier else "")
            self.field_brands.setText(supplier.brands if supplier else "")
            self.field_categories.setText(supplier.categories if supplier else "")
            self.field_contact.setText(supplier.contact if supplier else "")
            self.field_note.setPlainText(supplier.note if supplier else "")
            self._fill_terms(supplier)
            self.field_aliases.setText(
                ", ".join(suppliers.aliases(supplier.id)) if supplier else "")
            self.card_hint.setText(
                f"заведён {supplier.created_at:%d.%m.%Y}" if supplier and supplier.created_at
                else "")
        finally:
            self._loading = False
        self._fill_layouts(supplier)
        self._fill_links(supplier)

    def _fill_layouts(self, supplier: Supplier | None) -> None:
        self.layouts_list.clear()
        self._layouts = suppliers.layouts(supplier.id) if supplier else []
        for layout in self._layouts:
            pairs = "\n".join(f"      {name}  ←  {column}"
                              for name, column in layout.profile.price_map.items())
            item = QListWidgetItem(f"{layout.summary}\n{pairs}" if pairs else layout.summary)
            item.setData(Qt.ItemDataRole.UserRole, layout.id)
            item.setToolTip("Заголовки прайса:\n" + "\n".join(layout.titles[:25]))
            self.layouts_list.addItem(item)
        if not self._layouts:
            self.layouts_list.addItem(QListWidgetItem(
                "Структур пока нет — они появятся после первого сравнения"))

    def _fill_links(self, supplier: Supplier | None) -> None:
        self._links = suppliers.links(supplier.id) if supplier else []
        self.links_table.set_items(self._links)

    def _touch(self) -> None:
        if not self._loading and self.current is not None:
            self.save_button.setEnabled(True)

    # --- закрепление ----------------------------------------------------------

    def claim_selected(self) -> None:
        """Заявить выделенных поставщиков своими."""
        keys = [row.key for row in self._selected_rows()]
        if not keys:
            self.notify("Выделите поставщиков в списке", ToastKind.WARNING)
            return
        self._run_directory(
            lambda: directory.claim(keys),
            lambda result: f"Заявлено поставщиков: {result.changed}"
            if result.changed else "Все выделенные уже заявлены")

    def withdraw_selected(self) -> None:
        keys = [row.key for row in self._selected_rows()]
        if not keys:
            return
        self._run_directory(
            lambda: directory.withdraw(keys),
            lambda result: f"Заявка отозвана: {result.changed}"
            if result.changed else "Отзывать нечего: закрепление зафиксировано")

    def fix_selected(self) -> None:
        rows = [row for row in self._selected_rows() if row.entry.has_draft]
        if not rows:
            self.notify("Среди выделенных нет заявок на подтверждение",
                        ToastKind.WARNING)
            return
        answer = QMessageBox.question(
            self, "Зафиксировать закрепление",
            f"Подтвердить заявки по {len(rows)} поставщикам?\n\n"
            "После фиксации менеджер не сможет снять закрепление сам.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            return
        keys = [row.key for row in rows]
        self._run_directory(
            lambda: directory.fix(keys),
            lambda result: f"Зафиксировано закреплений: {result.changed}")

    def _run_directory(self, action: Callable[[], directory.Result],
                       message: Callable[[directory.Result], str]) -> None:
        """Массовое действие с закреплением: выполнить и перечитать список."""
        def done(result: directory.Result) -> None:
            self.notify(message(result), ToastKind.SUCCESS if result.changed
                        else ToastKind.INFO)
            self.reload()

        run_task(action, on_result=done,
                 on_error=lambda error: self.notify(
                     f"Не удалось изменить закрепление: {error}", ToastKind.ERROR))

    # --- карточка -------------------------------------------------------------

    def add_supplier(self) -> None:
        # Если в списке выбран поставщик без карточки, заводим её сразу на
        # него: имя из 1С точнее набранного заново, и карточка свяжется с
        # оплатами по нему же.
        chosen = self.chosen
        name = (chosen.name if chosen is not None and chosen.card is None
                else "Новый поставщик")
        supplier = Supplier(name=name)
        try:
            saved = suppliers.save_supplier(supplier)
        except Exception as error:  # noqa: BLE001
            self.notify(f"Не удалось создать карточку: {error}", ToastKind.ERROR)
            return
        self.current = saved
        self.reload()
        self.tabs.setCurrentIndex(0)
        self.field_name.setFocus()
        self.field_name.selectAll()

    def save_current(self) -> None:
        if self.current is None:
            return
        name = self.field_name.text().strip()
        if not name:
            self.notify("У поставщика должно быть имя", ToastKind.WARNING)
            return
        self.current.name = name
        self.current.brands = self.field_brands.text().strip()
        self.current.categories = self.field_categories.text().strip()
        self.current.contact = self.field_contact.text().strip()
        self.current.note = self.field_note.toPlainText().strip()
        self.current.payment_terms_days = _terms_value(self.field_terms)
        try:
            suppliers.save_supplier(self.current)
            suppliers.set_aliases(
                self.current.id,
                [part for part in self.field_aliases.text().split(",") if part.strip()])
        except Exception as error:  # noqa: BLE001
            self.notify(f"Не удалось сохранить: {error}", ToastKind.ERROR)
            return
        self.save_button.setEnabled(False)
        self.notify(f"Карточка «{name}» сохранена", ToastKind.SUCCESS)
        self.reload()

    def delete_supplier(self) -> None:
        if self.current is None:
            return
        answer = QMessageBox.question(
            self, "Удалить поставщика",
            f"Удалить «{self.current.name}» вместе со структурами прайсов "
            f"и {self.current.links} привязками?\n\nОтменить это будет нельзя.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        name = self.current.name
        suppliers.delete_supplier(self.current.id)
        self.current = None
        self.reload()
        self.notify(f"Поставщик «{name}» удалён", ToastKind.INFO)

    def add_layout(self) -> None:
        """Разбирает файл поставщика и запоминает его структуру."""
        if self.current is None:
            self.notify("Сначала выберите поставщика", ToastKind.WARNING)
            return
        self._edit(None)

    def edit_layout(self) -> None:
        row = self.layouts_list.currentRow()
        if self.current is None or not 0 <= row < len(self._layouts):
            self.notify("Выберите структуру в списке", ToastKind.WARNING)
            return
        self._edit(self._layouts[row])

    def _edit(self, layout: SupplierLayout | None) -> None:
        dialog = SupplierLayoutDialog(
            self.current, layout, suppliers.known_price_types(), self)
        if not dialog.exec():
            return
        try:
            suppliers.save_layout(dialog.result_layout())
        except Exception as error:  # noqa: BLE001
            self.notify(f"Структура не сохранена: {error}", ToastKind.ERROR)
            return
        self._fill_layouts(self.current)
        name = f" из «{dialog.file_name}»" if dialog.file_name else ""
        self.notify(
            f"Структура прайса сохранена{name}" if layout is None
            else "Структура прайса обновлена", ToastKind.SUCCESS)
        self.reload()

    def delete_layout(self) -> None:
        row = self.layouts_list.currentRow()
        if not 0 <= row < len(self._layouts):
            self.notify("Выберите структуру в списке", ToastKind.WARNING)
            return
        suppliers.delete_layout(self._layouts[row].id)
        self._fill_layouts(self.current)
        self.notify("Структура забыта — следующий прайс разберётся заново", ToastKind.INFO)
        self.reload()

    def delete_link(self) -> None:
        link = self.links_table.current_item()
        if not isinstance(link, SupplierLink):
            self.notify("Выберите привязку в таблице", ToastKind.WARNING)
            return
        suppliers.delete_link(link.id)
        self._fill_links(self.current)
        self.notify("Привязка снята", ToastKind.INFO)
        self.reload()

    def clear_links(self) -> None:
        if self.current is None or not self._links:
            return
        answer = QMessageBox.question(
            self, "Снять все привязки",
            f"Снять все {len(self._links)} привязок поставщика «{self.current.name}»?\n\n"
            "Позиции снова придётся сводить вручную.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = suppliers.clear_links(self.current.id)
        self._fill_links(self.current)
        self.notify(f"Снято привязок: {removed}", ToastKind.INFO)
        self.reload()

    def focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def show_supplier(self, supplier_id: int) -> None:
        """Открывает карточку конкретного поставщика — переход со страницы цен."""
        self.current = suppliers.get_supplier(supplier_id)
        if self.current is not None:
            # Поиском, а не выделением строки: поставщик мог не попасть на
            # текущую страницу отбора, и выделять было бы нечего. Сигналы
            # заглушены, иначе поле само запустит отложенный запрос вдогонку.
            self.search.blockSignals(True)
            self.search.setText(self.current.name)
            self.search.blockSignals(False)
        self._page_no = 1
        self.reload()


def _own_direction(directions: list[directory.Direction]) -> str:
    """Направление вошедшего — умолчание отбора, а не ограничение доступа.

    Приходит вместе с токеном. Ведёт человек оба отдела или ни одного — отбор
    остаётся на «Все»: гадать, какой из двух ему сейчас нужен, не следует, а
    новичку без направления правильнее показать всё.
    """
    own = [code for code in transport.session.directions
           if any(item.code == code for item in directions)]
    return own[0] if len(own) == 1 else ""


def _terms_value(box: SelectBox) -> int:
    """Отсрочка из поля: выбранный вариант или введённое число дней."""
    if (data := box.currentData()) is not None and box.currentText() == box.itemText(
            box.currentIndex()):
        return int(data)
    digits = "".join(ch for ch in box.currentText() if ch.isdigit())
    return int(digits) if digits else 0


def _history_terms(supplier_id: int, name: str) -> float:
    """Медианная отсрочка поставщика по базе оплат.

    Оплаты живут в своей базе, и её отсутствие не должно мешать карточке
    поставщика: любая беда с ней означает просто отсутствие подсказки.
    """
    try:
        from ..core.payments import planning, store as payments_store

        rows = payments_store.list_payments(payments_store.Filter(supplier_id=supplier_id))
        if not rows:
            rows = payments_store.list_payments(payments_store.Filter(recipient=name))
        return planning.payment_terms(rows)
    except Exception:  # noqa: BLE001 — подсказка необязательна
        return 0.0
