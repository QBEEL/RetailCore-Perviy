"""Вкладка «Поставщики»: карточки, структуры прайсов и сохранённые привязки.

У каждого поставщика свой файл переоценки и своя структура данных. База помнит
разбор каждого — как читать его прайс и какие позиции пришлось свести вручную, —
поэтому второй прайс того же поставщика обрабатывается почти без ручной работы.

Список строится из оплат, а не из карточек. Карточку заводят руками и только
когда нужен разбор прайса, поэтому их единицы; получатель же приходит с каждой
выгрузкой 1С, и таких — почти шестьсот. Список из карточек показывал бы пустую
вкладку при полутора тысячах реальных поставщиков.

Отбор по менеджеру сделан фильтром, а не разделением на «своих» и «чужих».
Половина получателей оплачивается несколькими людьми, и назначение каждому
единственного владельца было бы выдумкой: поставщик виден у всех, кто с ним
работал, а первым в подписи стоит тот, у кого оплат больше.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
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
from ..core.payments import SupplierRow, transport
from ..core.payments import data as payments
from ..core.suppliers import Supplier, SupplierLayout, SupplierLink
from ..core.normalize import normalize_text
from ..core.settings import AppSettings
from . import icons
from .tasks import run_task
from .theme import Metrics, Palette
from .widgets.common import Card, Divider, Hint, MetricTile, SectionTitle, Subtitle, Title, fade_in
from .widgets.inputs import SelectBox
from .widgets.supplier_dialogs import SupplierLayoutDialog
from .widgets.table import Column, DataTable
from .widgets.toast import ToastKind


@dataclass(slots=True)
class Entry:
    """Строка списка: поставщик из оплат и его карточка, если она заведена."""

    name: str = ""
    row: SupplierRow | None = None
    card: Supplier | None = None
    # Менеджер, по которому отобран список. Он должен стоять в подписи первым:
    # иначе при фильтре «мои» у поставщика значится чужое имя — то, у кого
    # оплат больше, — и строка выглядит попавшей в список по ошибке.
    focus: str = ""

    @property
    def id(self) -> int:
        return self.card.id if self.card else 0

    @property
    def managers(self) -> list[str]:
        return self.row.managers if self.row else []

    @property
    def manager_title(self) -> str:
        names = self.managers
        if not names:
            return ""
        if self.focus and self.focus in names:
            others = len(names) - 1
            return f"{self.focus} + ещё {others}" if others else self.focus
        return self.row.manager_title if self.row else ""

    @property
    def summary(self) -> str:
        parts: list[str] = []
        if self.row and self.row.payments:
            parts.append(f"оплат: {self.row.payments}")
            parts.append(f"{self.row.amount:,.0f} ₽".replace(",", " "))
            if self.row.last_pay:
                parts.append(f"последняя {self.row.last_pay:%d.%m.%Y}")
            if self.managers:
                parts.append(self.manager_title)
        if self.card:
            parts.append(f"структур: {self.card.layouts}")
        else:
            # Отсутствие карточки — не изъян: она нужна только для разбора
            # прайса. Но знать об этом, глядя на список, полезно.
            parts.append("без карточки")
        return " · ".join(parts)

    @property
    def haystack(self) -> str:
        card = self.card
        extra = f"{card.brands} {card.categories} {card.note}" if card else ""
        return f"{self.name} {extra} {' '.join(self.managers)}"


def _load_all(responsible: str) -> tuple[list[Supplier], list[SupplierRow]]:
    """Карточки и поставщики из оплат — одним походом, в фоновой задаче."""
    cards = suppliers.list_suppliers()
    try:
        rows = payments.suppliers(responsible)
    except Exception:  # noqa: BLE001 — оплаты не должны ронять вкладку
        # Нет входа в общую базу или сервер недоступен: карточки показать
        # всё равно можно, и это лучше пустого экрана с ошибкой.
        rows = []
    return cards, rows


def _merge(cards: list[Supplier], rows: list[SupplierRow],
           focus: str = "") -> list[Entry]:
    """Сводит поставщиков из оплат с карточками по нормализованному имени."""
    by_key = {card.key: card for card in cards}
    entries: list[Entry] = []
    used: set[str] = set()
    for row in rows:
        key = normalize_text(row.recipient)
        card = by_key.get(key)
        if card is not None:
            used.add(key)
        entries.append(Entry(name=row.recipient, row=row, card=card,
                             focus=focus))
    if focus:
        # При отборе по менеджеру чужие карточки не показываются: человек
        # просил своих поставщиков, а карточка без оплат под его именем к ним
        # не относится. Без отбора они видны — в них лежит разбор прайса.
        return entries
    for card in cards:
        if card.key not in used:
            entries.append(Entry(name=card.name, row=None, card=card))
    return entries


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
        self.entries: list[Entry] = []
        self.current: Supplier | None = None
        self.chosen: Entry | None = None
        self._layouts: list[SupplierLayout] = []
        self._links: list[SupplierLink] = []
        self._loading = False
        self._build()
        self.reload()

    # --- интерфейс ------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Title("Поставщики", self))
        root.addWidget(Subtitle(
            "База помнит структуру прайса каждого поставщика и позиции, которые "
            "пришлось свести вручную. Поставщик узнаётся по имени файла и по "
            "набору заголовков, поэтому следующий прайс разбирается сам.", self))

        root.addWidget(self._metrics_row())
        root.addWidget(self._workspace(), 1)

    def _metrics_row(self) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Metrics.GAP)
        self.tile_suppliers = MetricTile("Поставщиков", Palette.PRIMARY, row)
        self.tile_layouts = MetricTile("Структур прайсов", Palette.INFO, row)
        self.tile_links = MetricTile("Ручных привязок", Palette.SUCCESS, row)
        self.tile_size = MetricTile("Размер базы", Palette.TEXT_MUTED, row)
        for tile in (self.tile_suppliers, self.tile_layouts, self.tile_links, self.tile_size):
            layout.addWidget(tile)
        return row

    def _workspace(self) -> QSplitter:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._list_card())
        splitter.addWidget(self._detail_card())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([360, 820])
        return splitter

    def _list_card(self) -> Card:
        card = Card(self, padding=Metrics.PAD - 4)
        body = card.body()
        body.setSpacing(Metrics.GAP - 4)
        body.addWidget(SectionTitle("Список", card))

        self.manager = SelectBox(card)
        self.manager.setToolTip(
            "Поставщик виден у каждого, кто ему платил: половина получателей "
            "оплачивается несколькими менеджерами")
        self.manager.currentIndexChanged.connect(lambda _: self.reload())
        body.addWidget(self.manager)

        self.search = QLineEdit(card)
        self.search.setPlaceholderText("Поиск по названию, бренду, менеджеру…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _: self._fill_list())
        body.addWidget(self.search)

        self.list = QListWidget(card)
        self.list.setMinimumHeight(140)
        self.list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.list.currentRowChanged.connect(self._on_selected)
        body.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(7)
        add = QPushButton("Добавить", card)
        add.setIcon(icons.icon("open"))
        add.setToolTip("Завести карточку поставщика вручную")
        add.clicked.connect(self.add_supplier)
        buttons.addWidget(add)

        remove = QPushButton("Удалить", card)
        remove.setObjectName("Danger")
        remove.setIcon(icons.icon("trash", Palette.DANGER))
        remove.setToolTip("Удалить карточку вместе со структурами и привязками")
        remove.clicked.connect(self.delete_supplier)
        buttons.addWidget(remove)
        body.addLayout(buttons)
        return card

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

    # --- данные ---------------------------------------------------------------

    def reload(self) -> None:
        """Читает список. Вызывается при каждом открытии вкладки."""
        chosen = self.manager.currentData() or ""
        run_task(
            lambda: _load_all(chosen),
            on_result=self._on_loaded,
            on_error=lambda message: self.notify(
                f"Список поставщиков недоступен: {message}", ToastKind.ERROR),
        )

    def _on_loaded(self, payload: tuple[list[Supplier], list[SupplierRow]]) -> None:
        cards, rows = payload
        self.items = cards
        self.entries = _merge(cards, rows, self.manager.currentData() or "")
        self._fill_managers(rows)

        previous = self.chosen.name if self.chosen else ""
        self._fill_list(select=previous)
        self.tile_suppliers.set_value(len(self.entries))
        self.tile_layouts.set_value(sum(s.layouts for s in cards))
        self.tile_links.set_value(sum(s.links for s in cards))
        self.tile_size.set_value(f"{suppliers.database_size() / 1024:.0f} КБ")
        fade_in(self.list)

    def _fill_managers(self, rows: list[SupplierRow]) -> None:
        """Список менеджеров. Свой — первым после «всех»."""
        if self.manager.count():
            return
        names = sorted({name for row in rows for name in row.managers})
        mine = transport.session.full_name
        self.manager.blockSignals(True)
        self.manager.addItem("Все менеджеры", "")
        if mine in names:
            self.manager.addItem(f"Мои — {mine}", mine)
            names.remove(mine)
        for name in names:
            self.manager.addItem(name, name)
        # Свои поставщики нужнее чужих: если вошли под учёткой, начинаем с них.
        if mine and self.manager.count() > 1 and self.manager.itemData(1) == mine:
            self.manager.setCurrentIndex(1)
        self.manager.blockSignals(False)
        if self.manager.currentIndex() == 1:
            self.reload()

    def _visible(self) -> list[Entry]:
        query = self.search.text().casefold().replace("ё", "е").strip()
        if not query:
            return self.entries
        return [entry for entry in self.entries
                if query in entry.haystack.casefold().replace("ё", "е")]

    def _fill_list(self, select: str = "") -> None:
        self._loading = True
        self.list.clear()
        visible = self._visible()
        for entry in visible:
            item = QListWidgetItem(f"{entry.name}\n{entry.summary}")
            item.setData(Qt.ItemDataRole.UserRole, entry.name)
            if entry.card is not None and not entry.card.active:
                item.setForeground(QColor(Palette.TEXT_FAINT))
            self.list.addItem(item)
        self._loading = False
        if not visible:
            self.chosen = None
            self._show(None)
            return
        position = next((i for i, e in enumerate(visible) if e.name == select), 0)
        self.list.setCurrentRow(position)

    def _on_selected(self, row: int) -> None:
        if self._loading:
            return
        visible = self._visible()
        entry = visible[row] if 0 <= row < len(visible) else None
        self.chosen = entry
        self._show(entry.card if entry else None)
        if entry is not None and entry.card is None:
            # Карточки нет — показать нечего, но человек должен видеть, что
            # выбрал, и понимать, чего именно не хватает.
            self.detail_title.setText(entry.name)
            self.field_name.setText(entry.name)

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

    # --- действия -------------------------------------------------------------

    def add_supplier(self) -> None:
        # Если в списке выбран поставщик из оплат без карточки, заводим её
        # сразу на него: имя из 1С точнее набранного заново, и карточка
        # свяжется с оплатами по нему же.
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
        self.current = Supplier(id=supplier_id)
        self.reload()


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
