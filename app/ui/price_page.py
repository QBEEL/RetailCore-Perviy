"""Вкладка «Быстрая смена цен»: новые цены поставщика в шаблон выгрузки 1С.

Ручной процесс — выгрузить шаблон, открыть прайс, найти товар, скопировать
цену — сводится к трём действиям: выбрать два файла, нажать «Сравнить»,
сохранить результат. Всё, что не удалось сопоставить уверенно, остаётся
видимым и правится вручную, а не подставляется молча.
"""
from __future__ import annotations

import os
from typing import Callable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core import pricing, suppliers, workbook
from ..core.models import FieldRole
from ..core.pricing import (
    ComparisonResult,
    OneCTemplate,
    PriceCell,
    PriceLine,
    PriceStats,
    PriceStatus,
    PriceType,
    SupplierPrice,
)
from ..core.settings import AppSettings
from ..core.suppliers import LinkKey, Session
from . import icons
from .snapshot_task import capture
from .tasks import run_task
from .theme import STATUS_COLORS, Metrics, Palette, score_color
from .widgets.common import Card, Divider, Hint, MetricTile, SectionTitle, Subtitle, Title, fade_in
from .widgets.file_picker import FilePicker
from .widgets.inputs import SelectBox
from .widgets.price_dialogs import PriceSettingsDialog
from .widgets.table import Column, DataTable
from .widgets.toast import ToastKind

_CANDIDATES_HINT = "Варианты из прайса поставщика · двойной клик — привязать"


class PricePage(QWidget):
    """Шаблон 1С слева, прайс поставщика справа, готовый файл на выходе."""

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, ToastKind], None],
        parent: QWidget | None = None,
        open_supplier: Callable[[int], None] | None = None,
        plan_payment: Callable[..., None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._open_supplier = open_supplier
        self._plan_payment = plan_payment
        self.template: OneCTemplate | None = None
        self.supplier: SupplierPrice | None = None
        self.session: Session = Session()
        self.result: ComparisonResult | None = None
        self._keys: list[LinkKey] = []
        self._mapping_boxes: list[tuple[str, SelectBox]] = []
        self._choices: list = []
        self._price_names: list[str] = []
        self._busy = False
        self._build()

    @property
    def profile(self):
        """Соответствие колонок текущего поставщика."""
        return self.session.profile

    # --- интерфейс ------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Title("Быстрая смена цен", self))
        root.addWidget(Subtitle(
            "Новые цены из прайса поставщика переносятся в шаблон выгрузки 1С. "
            "Товар ищется по артикулу, коду и названию; вариант нужного объёма "
            "выбирается автоматически. Итоговый файл повторяет шаблон — "
            "меняются только колонки «Цена».", self))

        root.addWidget(self._files_card())
        root.addWidget(self._metrics_row())
        root.addWidget(self._workspace(), 1)

    def _files_card(self) -> Card:
        card = Card(self)
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        body = card.body()

        self.template_picker = FilePicker("Шаблон выгрузки из 1С", "не выбран", card)
        self.template_picker.file_selected.connect(lambda p: self._load(p, template=True))
        self.template_picker.sheet_changed.connect(lambda s: self._reload(template=True, sheet=s))
        self.template_picker.set_recent(self.settings.recent_price_template)
        body.addWidget(self.template_picker)

        body.addWidget(Divider(card))

        self.supplier_picker = FilePicker("Файл переоценки поставщика", "не выбран", card)
        self.supplier_picker.file_selected.connect(lambda p: self._load(p, template=False))
        self.supplier_picker.sheet_changed.connect(lambda s: self._reload(template=False, sheet=s))
        self.supplier_picker.set_recent(self.settings.recent_price_supplier)
        body.addWidget(self.supplier_picker)

        body.addWidget(Divider(card))
        body.addWidget(self._mapping_block(card))
        body.addWidget(Divider(card))
        body.addLayout(self._actions(card))
        return card

    def _mapping_block(self, card: Card) -> QWidget:
        """Соответствие видов цены 1С колонкам поставщика — сердце модуля."""
        block = QWidget(card)
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Какая цена поставщика идёт в какой вид цены 1С", block))
        self.profile_label = Hint("", block)
        header.addWidget(self.profile_label, 1)
        self.profiles_button = QPushButton("Поставщик", block)
        self.profiles_button.setIcon(icons.icon("suppliers"))
        self.profiles_button.setToolTip("Открыть карточку поставщика в базе")
        self.profiles_button.clicked.connect(self.open_supplier_card)
        header.addWidget(self.profiles_button)
        layout.addLayout(header)

        self.mapping_grid = QGridLayout()
        self.mapping_grid.setHorizontalSpacing(Metrics.GAP)
        self.mapping_grid.setVerticalSpacing(5)
        self.mapping_grid.setColumnStretch(1, 1)
        layout.addLayout(self.mapping_grid)

        self.mapping_hint = Hint("Выберите оба файла — соответствие подберётся само.", block)
        layout.addWidget(self.mapping_hint)
        self.profiles_button.setEnabled(False)
        return block

    def _actions(self, card: Card) -> QHBoxLayout:
        actions = QHBoxLayout()
        actions.setSpacing(9)

        self.run_button = QPushButton("Сравнить", card)
        self.run_button.setObjectName("Primary")
        self.run_button.setIcon(icons.icon("run", Palette.TEXT_ON_PRIMARY))
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.run_comparison)
        actions.addWidget(self.run_button)

        self.export_button = QPushButton("Экспорт в 1С", card)
        self.export_button.setObjectName("Success")
        self.export_button.setIcon(icons.icon("save", Palette.TEXT_ON_PRIMARY))
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export)
        actions.addWidget(self.export_button)

        self.payment_button = QPushButton("Создать оплату", card)
        self.payment_button.setIcon(icons.icon("payments"))
        self.payment_button.setToolTip(
            "Открывает карточку оплаты: поставщик и дата по отсрочке уже заполнены")
        self.payment_button.setEnabled(False)
        self.payment_button.clicked.connect(self.plan_payment)
        actions.addWidget(self.payment_button)

        settings_button = QPushButton("Настройки", card)
        settings_button.setIcon(icons.icon("settings"))
        settings_button.setToolTip("Поля поиска, порог совпадения, разделители артикула")
        settings_button.clicked.connect(self.edit_settings)
        actions.addWidget(settings_button)

        self.progress = QProgressBar(card)
        self.progress.setFixedWidth(180)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        actions.addWidget(self.progress)

        self.status_label = Hint("Выберите шаблон 1С и файл поставщика", card)
        actions.addWidget(self.status_label, 1)
        return actions

    def _metrics_row(self) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Metrics.GAP)
        self.tile_total = MetricTile("Всего товаров", Palette.TEXT_MUTED, row)
        self.tile_found = MetricTile("Найдено", Palette.PRIMARY, row)
        self.tile_missing = MetricTile("Не найдено", Palette.DANGER, row)
        self.tile_changed = MetricTile("Изменено цен", Palette.SUCCESS, row)
        self.tile_same = MetricTile("Без изменений", Palette.INFO, row)
        self.tile_rate = MetricTile("Процент совпадения", Palette.WARNING, row)
        for tile in (self.tile_total, self.tile_found, self.tile_missing,
                     self.tile_changed, self.tile_same, self.tile_rate):
            layout.addWidget(tile)
        return row

    def _workspace(self) -> QSplitter:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._results_card())
        splitter.addWidget(self._manual_card())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([860, 340])
        return splitter

    def _results_card(self) -> Card:
        card = Card(self, padding=Metrics.PAD - 4)
        body = card.body()
        body.setSpacing(Metrics.GAP - 4)

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Результат сравнения", card))
        self.filter_box = SelectBox(card)
        self.filter_box.addItem("Все", "")
        for status in (PriceStatus.CHANGED, PriceStatus.UNCHANGED, PriceStatus.REVIEW,
                       PriceStatus.NO_PRICE, PriceStatus.NOT_FOUND):
            self.filter_box.addItem(status.title, status.value)
        self.filter_box.currentIndexChanged.connect(
            lambda: self.table.proxy.set_status(self.filter_box.currentData()))
        header.addWidget(self.filter_box)

        self.search = QLineEdit(card)
        self.search.setPlaceholderText("Поиск по результату…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        header.addWidget(self.search, 1)
        body.addLayout(header)

        self.table = DataTable(self._columns(), card)
        self.table.setMinimumHeight(120)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.table.model_.set_status_provider(lambda line: line.status.value)
        self.table.selectionModel().selectionChanged.connect(self._on_selected)
        body.addWidget(self.table, 1)
        return card

    def _columns(self, types: Sequence[PriceType] = ()) -> list[Column]:
        return [
            Column("Статус", lambda l: l.status.title, 150,
                   sort_key=lambda l: l.status.value,
                   color=lambda l: QColor(STATUS_COLORS[
                       pricing.PRICE_STATUS_TONES[l.status]][0])),
            Column("Артикул 1С", lambda l: l.article, 170, highlight=True),
            Column("Товар", lambda l: l.name, 260, highlight=True),
            # Цены — сразу за товаром: ради них таблицу и смотрят, и до
            # розничной не должно быть нужно листать вправо.
            *[column for index, price_type in enumerate(types)
              for column in _price_columns(index, price_type.name)],
            Column("Артикул поставщика", lambda l: l.supplier_article, 150, highlight=True),
            Column("Способ", _method_text, 150,
                   sort_key=lambda l: l.method,
                   color=lambda l: QColor(Palette.WARNING) if l.link_warning
                   else QColor(Palette.PRIMARY) if l.linked else None),
            Column("Оценка", lambda l: f"{l.score:.0f}" if l.matched else "", 74,
                   sort_key=lambda l: l.score,
                   align=Qt.AlignmentFlag.AlignCenter,
                   color=lambda l: score_color(l.score) if l.matched else None),
            Column("Строка", lambda l: l.row, 74,
                   sort_key=lambda l: l.row,
                   align=Qt.AlignmentFlag.AlignCenter),
        ]

    def _manual_card(self) -> Card:
        card = Card(self, padding=Metrics.PAD - 4)
        body = card.body()
        body.setSpacing(Metrics.GAP - 4)
        body.addWidget(SectionTitle("Ручное сопоставление", card))

        self.selected_label = QLabel("Выберите строку слева", card)
        self.selected_label.setWordWrap(True)
        self.selected_label.setStyleSheet(f"color: {Palette.TEXT_MUTED};")
        body.addWidget(self.selected_label)

        self.prices_label = Hint("", card)
        body.addWidget(self.prices_label)

        self.candidates_hint = Hint(_CANDIDATES_HINT, card)
        body.addWidget(self.candidates_hint)

        self.candidate_list = QListWidget(card)
        self.candidate_list.setMinimumHeight(90)
        self.candidate_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.candidate_list.itemDoubleClicked.connect(lambda _: self.apply_manual())
        body.addWidget(self.candidate_list, 1)

        self.remember_box = QCheckBox("Запоминать привязку для поставщика", card)
        self.remember_box.setChecked(True)
        self.remember_box.setToolTip(
            "Привязка сохранится в базе поставщиков и применится к следующему\n"
            "его прайсу автоматически — эту позицию не придётся сводить заново.")
        body.addWidget(self.remember_box)

        buttons = QHBoxLayout()
        buttons.setSpacing(7)
        bind = QPushButton("Привязать", card)
        bind.setObjectName("Primary")
        bind.setIcon(icons.icon("link", Palette.TEXT_ON_PRIMARY))
        bind.setToolTip("Взять цену выбранного варианта")
        bind.clicked.connect(self.apply_manual)
        buttons.addWidget(bind)

        clear = QPushButton("Снять", card)
        clear.setObjectName("Danger")
        clear.setIcon(icons.icon("unlink", Palette.DANGER))
        clear.setToolTip("Оставить старую цену этой строки")
        clear.clicked.connect(self.clear_manual)
        buttons.addWidget(clear)
        body.addLayout(buttons)

        export_missing = QPushButton("Выгрузить несопоставленные", card)
        export_missing.setIcon(icons.icon("export"))
        export_missing.setToolTip(
            "Список позиций, которых нет в прайсе, — чтобы запросить их у поставщика")
        export_missing.clicked.connect(self.export_missing)
        body.addWidget(export_missing)
        return card

    # --- загрузка файлов ------------------------------------------------------

    def _load(self, path: str, *, template: bool) -> None:
        picker = self.template_picker if template else self.supplier_picker
        picker.set_status("чтение файла…", Palette.PRIMARY)
        try:
            sheets = workbook.list_sheets(path)
        except Exception as error:  # noqa: BLE001 — сообщение уходит в уведомление
            self._on_failed(str(error), template)
            return
        remembered = self.settings.price_sheet_for(path)
        picker.set_sheets(sheets, remembered)
        self.settings.remember_price_file(path, template=template)
        picker.set_recent(self.settings.recent_price_template if template
                          else self.settings.recent_price_supplier)
        self._reload(template=template, sheet=remembered or None)

    def _reload(self, *, template: bool, sheet: str | None) -> None:
        picker = self.template_picker if template else self.supplier_picker
        if not picker.path:
            return
        picker.set_status("разбор листа…", Palette.PRIMARY)
        self._set_busy(True)
        arguments = (picker.path, sheet)
        if not template:
            # Профиль может задавать роли колонок вручную — они нужны уже при чтении.
            arguments += (self.settings.supplier_profiles.for_file(picker.path),)
        run_task(
            pricing.read_template if template else pricing.read_supplier,
            *arguments,
            on_result=lambda parsed: self._on_loaded(parsed, template),
            on_error=lambda message: self._on_failed(message, template),
            on_progress=self._on_progress,
        )

    def _on_loaded(self, parsed, template: bool) -> None:
        self._set_busy(False)
        picker = self.template_picker if template else self.supplier_picker
        if template:
            self.template = parsed
            types = ", ".join(t.name for t in parsed.valid_types)
            picker.set_status(
                f"лист «{parsed.sheet_name}» · {len(parsed.records)} строк · "
                f"виды цен: {types}", Palette.SUCCESS)
        else:
            self.supplier = parsed
            picker.set_status(
                f"лист «{parsed.sheet_name}» · {len(parsed.records)} строк · "
                f"ценовых колонок: {len(parsed.price_columns)}", Palette.SUCCESS)
            # Прайс поставщика — это каталог, и он идёт в историю данных наравне
            # с прайсами других страниц: по нему потом видно, как менялись цены.
            capture(parsed.as_sheet(), self.settings, self.notify,
                    lambda text: picker.set_status(text, Palette.PRIMARY) if text else None)
        _select_sheet(picker, parsed.sheet_name)
        self.settings.remember_price_sheet(parsed.path, parsed.sheet_name)
        self.settings.save()
        self._reset_result()
        self._refresh_mapping()
        self._update_ready()

    def _on_failed(self, message: str, template: bool) -> None:
        self._set_busy(False)
        picker = self.template_picker if template else self.supplier_picker
        picker.set_status("ошибка чтения", Palette.DANGER)
        if template:
            self.template = None
        else:
            self.supplier = None
        self._update_ready()
        self.notify(f"Не удалось прочитать файл: {message}", ToastKind.ERROR)

    # --- соответствие колонок -------------------------------------------------

    def _refresh_mapping(self) -> None:
        """Перестраивает строки соответствия под текущую пару файлов."""
        while self.mapping_grid.count():
            if widget := self.mapping_grid.takeAt(0).widget():
                widget.deleteLater()
        self._mapping_boxes = []

        if self.template is None or self.supplier is None:
            self.mapping_hint.setVisible(True)
            self.mapping_hint.setText("Выберите оба файла — соответствие подберётся само.")
            self.profile_label.setText("")
            self.profiles_button.setEnabled(False)
            return

        try:
            self.session = suppliers.open_session(self.template, self.supplier)
        except Exception as error:  # noqa: BLE001 — без базы страница обязана работать
            self.notify(f"База поставщиков недоступна: {error}", ToastKind.WARNING)
            self.session = Session()
        self._show_profile_label()

        for row, price_type in enumerate(self.template.valid_types):
            label = QLabel(price_type.name, self)
            label.setStyleSheet("font-weight: 500;")
            box = SelectBox(self)
            box.setMinimumWidth(240)
            box.addItem("— не заполнять —", "")
            for column in self.supplier.price_columns:
                box.addItem(column.label, column.title)
            chosen = self.profile.column_for(price_type)
            box.setCurrentIndex(max(box.findData(chosen), 0))
            box.currentIndexChanged.connect(self._on_mapping_changed)
            self.mapping_grid.addWidget(label, row, 0)
            self.mapping_grid.addWidget(box, row, 1)
            self._mapping_boxes.append((price_type.name, box))

        self.mapping_hint.setVisible(not self.supplier.price_columns)
        if not self.supplier.price_columns:
            self.mapping_hint.setText(
                "В прайсе поставщика не найдено числовых колонок с ценой — "
                "проверьте выбранный лист.")

    def _show_profile_label(self) -> None:
        supplier = self.session.supplier
        if not supplier.name:
            self.profile_label.setText("")
            self.profiles_button.setEnabled(False)
            return
        links = f" · привязок {len(self.session.book)}" if self.session.book else ""
        state = self.session.reason if self.session.known else "новый поставщик · будет сохранён"
        self.profile_label.setText(f"«{supplier.name}» — {state}{links}")
        self.profiles_button.setEnabled(bool(supplier.id))
        self.payment_button.setEnabled(bool(supplier.name) and self._plan_payment is not None)

    def _on_mapping_changed(self) -> None:
        self.profile.price_map = {
            name: box.currentData() for name, box in self._mapping_boxes if box.currentData()
        }
        # Цены пересчитываются на месте: подбор товара от колонки не зависит.
        if self.result is not None and self.template and self.supplier:
            stats = pricing.recompare(self.result.lines, self.template, self.supplier, self.profile)
            self._show_stats(stats)
            self.table.set_items(self.result.lines)
            self._on_selected()

    def _update_ready(self) -> None:
        ready = self.template is not None and self.supplier is not None and not self._busy
        self.run_button.setEnabled(ready)
        if ready:
            self.status_label.setText("Готово — нажмите «Сравнить»")
        elif self.template is None or self.supplier is None:
            self.status_label.setText("Выберите шаблон 1С и файл поставщика")

    # --- сравнение ------------------------------------------------------------

    def run_comparison(self) -> None:
        if not (self.template and self.supplier) or self._busy:
            return
        if not self.profile.price_map:
            self.notify("Не выбрана ни одна колонка с ценой поставщика", ToastKind.WARNING)
            return
        self._set_busy(True)
        self.status_label.setText("Сопоставление товаров…")
        run_task(
            suppliers.run_comparison,
            self.template,
            self.supplier,
            self.session,
            self.settings.price_match,
            on_result=self._on_compared,
            on_error=self._on_compare_failed,
            on_progress=self._on_progress,
        )

    def _on_compared(self, result: ComparisonResult) -> None:
        self._set_busy(False)
        self.result = result
        self._keys = suppliers.keys_for(self.template, result.lines)
        self._show_price_columns(result.types)
        self.table.set_items(result.lines)
        self._show_stats(result.stats)
        fade_in(self.table)
        self.export_button.setEnabled(result.stats.changed > 0)
        self._remember_session()

        stats = result.stats
        linked = sum(1 for line in result.lines if line.linked)
        self.status_label.setText(
            f"Найдено {stats.found} из {stats.total} ({stats.rate:.1f} %) · "
            f"новых цен {stats.changed}"
            + (f" · из привязок {linked}" if linked else ""))
        if stats.review or stats.not_found:
            self.notify(
                f"Требует сопоставления: {stats.review + stats.not_found} позиций — "
                "выберите вариант в правой панели",
                ToastKind.WARNING)
        elif stats.changed:
            self.notify(f"Готово: новых цен {stats.changed}", ToastKind.SUCCESS)
        else:
            self.notify("Цены совпадают — менять нечего", ToastKind.INFO)

        if warnings := sum(1 for line in result.lines if line.link_warning):
            self.notify(
                f"У {warnings} сохранённых привязок разошлись названия — "
                "проверьте, не сменился ли товар за артикулом",
                ToastKind.WARNING)

    def _show_price_columns(self, types: Sequence[PriceType]) -> None:
        """Колонки под виды цен шаблона — они известны только после сравнения.

        Раньше в таблице стояла одна цена — первого вида, обычно
        себестоимости, — и чтобы увидеть, как изменилась РРЦ, приходилось
        открывать каждую строку. Набор меняется, только если поменялись сами
        виды: иначе повторное сравнение сбрасывало бы подогнанные ширины.
        """
        names = [price_type.name for price_type in types]
        if names != self._price_names:
            self._price_names = names
            self.table.set_columns(self._columns(types))

    def _remember_session(self) -> None:
        """Запоминает поставщика и структуру его прайса после успешного сравнения."""
        if self.supplier is None:
            return
        try:
            self.session = suppliers.remember_session(self.session, self.supplier)
        except Exception as error:  # noqa: BLE001 — сравнение уже состоялось
            self.notify(f"Поставщик не сохранён: {error}", ToastKind.WARNING)
            return
        self._show_profile_label()

    def _on_compare_failed(self, message: str) -> None:
        self._set_busy(False)
        self.status_label.setText("Сравнение не выполнено")
        self.notify(f"Ошибка сравнения: {message}", ToastKind.ERROR)

    def _show_stats(self, stats: PriceStats) -> None:
        self.tile_total.set_value(stats.total)
        self.tile_found.set_value(stats.found)
        self.tile_missing.set_value(stats.not_found + stats.review)
        self.tile_changed.set_value(stats.changed)
        self.tile_same.set_value(stats.unchanged)
        self.tile_rate.set_value(f"{stats.rate:.1f} %")

    def _reset_result(self) -> None:
        self.result = None
        self.table.set_items([])
        self.candidate_list.clear()
        self.export_button.setEnabled(False)
        self._show_stats(PriceStats())

    # --- ручное сопоставление -------------------------------------------------

    def _on_selected(self) -> None:
        line = self.table.current_item()
        if not isinstance(line, PriceLine):
            return
        source = line.source
        target = (f"→ {source.text(FieldRole.ARTICLE)} · {source.label}"
                  if source else "→ вариант не выбран")
        self.selected_label.setText(
            f"{line.name}\nартикул {line.article} · строка {line.row}\n{target}")
        self.selected_label.setStyleSheet(f"color: {Palette.TEXT}; font-weight: 500;")
        self.prices_label.setText(self._prices_text(line))
        self._fill_candidates(line)

    def _prices_text(self, line: PriceLine) -> str:
        if not (self.result and line.cells):
            return ""
        rows = []
        for cell in line.cells:
            name = self.result.types[cell.type_index].name
            mark = "изменится" if cell.changed else "без изменений"
            rows.append(f"{name}: {_money(cell.old)} → {_money(cell.new)}  ({mark})")
        return "\n".join(rows)

    def _fill_candidates(self, line: PriceLine) -> None:
        self._choices = list(pricing.choices(line))
        self.candidate_list.clear()
        self.candidates_hint.setText(
            _CANDIDATES_HINT if self._choices else "Вариантов не найдено")
        for candidate in self._choices:
            record = candidate.record
            chosen = "✓ " if candidate is line.candidate else ""
            volume = f" · {record.quantity}" if record.quantity else ""
            item = QListWidgetItem(
                f"{chosen}{record.text(FieldRole.ARTICLE)}  ·  {record.label[:56]}{volume}")
            item.setToolTip(
                f"{record.label}\nартикул {record.text(FieldRole.ARTICLE)}"
                f"\nстрока прайса {record.row} · {candidate.stage}"
                f"\nоценка {candidate.score:.1f}"
                + ("\n⚠ объём не совпадает" if candidate.volume_conflict else ""))
            if candidate.volume_conflict:
                item.setForeground(QColor(Palette.WARNING))
            self.candidate_list.addItem(item)
        if not self._choices:
            self.candidate_list.addItem(QListWidgetItem(
                "Товара нет в прайсе поставщика — старая цена останется без изменений"))

    def apply_manual(self) -> None:
        line = self.table.current_item()
        row = self.candidate_list.currentRow()
        if not isinstance(line, PriceLine):
            self.notify("Выберите строку в таблице", ToastKind.WARNING)
            return
        if not 0 <= row < len(self._choices):
            self.notify("Выберите вариант в списке справа", ToastKind.WARNING)
            return
        line.assign(self._choices[row])
        remembered = self._remember_link(line)
        note = " · сохранено для поставщика" if remembered else ""
        self._after_manual(
            line, f"Привязано к «{self._choices[row].record.label[:50]}»{note}")

    def _remember_link(self, line: PriceLine) -> bool:
        """Сохраняет привязку в базе, если пользователь этого хочет."""
        if not self.remember_box.isChecked() or not self.session.supplier.id:
            return False
        key = self._key_of(line)
        if not key:
            return False
        try:
            return suppliers.remember_link(line, key, self.session)
        except Exception as error:  # noqa: BLE001 — привязка в этом прогоне уже применена
            self.notify(f"Привязка не сохранена: {error}", ToastKind.WARNING)
            return False

    def _key_of(self, line: PriceLine) -> LinkKey | None:
        try:
            return self._keys[self.result.lines.index(line)] if self.result else None
        except ValueError:
            return None

    def clear_manual(self) -> None:
        line = self.table.current_item()
        if not isinstance(line, PriceLine):
            return
        forgotten = False
        if line.linked and (key := self._key_of(line)) and self.session.supplier.id:
            forgotten = suppliers.forget_link(key, self.session)
        line.clear()
        note = " · привязка удалена из базы" if forgotten else ""
        self._after_manual(line, f"Привязка снята — цена останется прежней{note}")

    def _after_manual(self, line: PriceLine, message: str) -> None:
        if not (self.result and self.template and self.supplier):
            return
        stats = pricing.recompare(self.result.lines, self.template, self.supplier, self.profile)
        self.result.stats = stats
        self._show_stats(stats)
        self.table.model_.refresh_item(line)
        self.export_button.setEnabled(stats.changed > 0)
        self.status_label.setText(
            f"Найдено {stats.found} из {stats.total} ({stats.rate:.1f} %) · "
            f"новых цен {stats.changed}")
        self._on_selected()
        self.notify(message, ToastKind.SUCCESS)

    def _filter(self, text: str) -> None:
        self.table.proxy.set_text(text)
        self.table.model_.set_terms(text.casefold().replace("ё", "е").split())

    # --- настройки и профили --------------------------------------------------

    def edit_settings(self) -> None:
        dialog = PriceSettingsDialog(
            self.settings.price_match, self.settings.price_skip_unchanged, self)
        if not dialog.exec():
            return
        dialog.apply_to(self.settings.price_match)
        self.settings.price_skip_unchanged = dialog.skip_unchanged
        self.settings.save()
        if self.result is not None:
            self.notify("Настройки сохранены — нажмите «Сравнить» ещё раз", ToastKind.INFO)

    def open_supplier_card(self) -> None:
        """Переход к карточке поставщика на вкладке «Поставщики»."""
        if self._open_supplier and self.session.supplier.id:
            self._open_supplier(self.session.supplier.id)

    # --- выгрузка -------------------------------------------------------------

    def export(self) -> None:
        if not (self.result and self.template) or self._busy:
            return
        suggested = pricing.default_export_path(self.template)
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить файл для загрузки в 1С", suggested,
            "Excel (*.xlsx);;Excel 1С (*.xls)")
        if not path:
            return
        self._set_busy(True)
        self.status_label.setText("Формирование файла…")
        run_task(
            pricing.save_result,
            self.template,
            self.result.lines,
            path,
            skip_unchanged=self.settings.price_skip_unchanged,
            on_result=self._on_exported,
            on_error=self._on_export_failed,
            on_progress=self._on_progress,
        )

    def _on_exported(self, report) -> None:
        self._set_busy(False)
        removed = f" · убрано строк: {report.removed}" if report.removed else ""
        self.status_label.setText(f"Сохранено: {report.path}")
        self.notify(
            f"Файл готов: {report.file_name} · новых цен {report.cells} "
            f"в {report.rows} строках{removed}", ToastKind.SUCCESS)

    def _on_export_failed(self, message: str) -> None:
        self._set_busy(False)
        self.status_label.setText("Файл не сохранён")
        self.notify(f"Не удалось сохранить: {message}", ToastKind.ERROR)

    def export_missing(self) -> None:
        if self.result is None:
            return
        missing = [line for line in self.result.lines
                   if line.status in (PriceStatus.REVIEW, PriceStatus.NOT_FOUND)]
        if not missing:
            self.notify("Все позиции сопоставлены — выгружать нечего", ToastKind.INFO)
            return
        base = os.path.dirname(self.template.path) if self.template else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить список", os.path.join(base, "Не сопоставлено.xlsx"),
            "Excel (*.xlsx)")
        if not path:
            return
        try:
            _write_missing(missing, path)
        except Exception as error:  # noqa: BLE001
            self.notify(f"Не удалось сохранить список: {error}", ToastKind.ERROR)
            return
        self.notify(f"Список сохранён: {os.path.basename(path)}", ToastKind.SUCCESS)

    # --- фон ------------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
        self.run_button.setEnabled(not busy and self.template is not None
                                   and self.supplier is not None)
        self.export_button.setEnabled(not busy and self.result is not None
                                      and self.result.stats.changed > 0)
        if not busy:
            self._update_ready()

    def _on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)


# --- вспомогательное ----------------------------------------------------------
    def plan_payment(self) -> None:
        """Открывает карточку оплаты поставщику этого прайса.

        Сумму подставить неоткуда: переоценка меняет цены, а не считает заказ.
        Поставщик и дата по отсрочке заполняются, сумму вводит пользователь.
        """
        if self._plan_payment is None or self.session is None:
            return
        supplier = self.session.supplier
        if not supplier.name:
            self.notify("Поставщик не определён — оплату не к кому отнести", ToastKind.WARNING)
            return
        changed = sum(1 for line in (self.result.lines if self.result else [])
                      if line.status is pricing.PriceStatus.CHANGED)
        note = f"Переоценка {os.path.basename(self.supplier_picker.path)}"
        if changed:
            note += f", изменено цен: {changed}"
        self._plan_payment(
            recipient=supplier.name,
            supplier_id=supplier.id,
            terms_days=supplier.payment_terms_days,
            comment=note,
            origin="pricing",
            origin_ref=self.supplier_picker.path,
        )


_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


def _price_columns(index: int, name: str) -> list[Column]:
    """Две колонки на вид цены: «было → стало» и изменение в процентах."""
    return [
        Column(name, lambda l: _price_text(l.cell(index)), max(130, 8 * len(name)),
               sort_key=lambda l: _price_sort(l.cell(index)),
               align=_RIGHT,
               color=lambda l: _price_color(l.cell(index))),
        Column("Δ %", lambda l: _percent_text(l.cell(index)), 78,
               sort_key=lambda l: _percent_sort(l.cell(index)),
               align=_RIGHT,
               color=lambda l: _percent_color(l.cell(index)),
               tip=f"Δ % {name}"),
    ]


def _price_text(cell: PriceCell | None) -> str:
    """«1 200 → 1 350», если цена меняется; одна цена — если нет."""
    if cell is None:
        return "—"
    if cell.changed:
        return f"{_money(cell.old)} → {_money(cell.new)}"
    return _money(cell.new if cell.new is not None else cell.old)


def _price_sort(cell: PriceCell | None) -> float:
    if cell is None:
        return 0.0
    return next((v for v in (cell.new, cell.old) if v is not None), 0.0)


def _price_color(cell: PriceCell | None) -> QColor | None:
    # Цены, которые поставщик не прислал, останутся прежними — бледные.
    if cell is not None and cell.new is None:
        return QColor(Palette.TEXT_FAINT)
    return None


def _method_text(line: PriceLine) -> str:
    """Способ подбора. Расхождение названий у привязки помечается прямо в таблице."""
    return f"{line.method} ⚠" if line.link_warning else line.method


def _percent_sort(cell: PriceCell | None) -> float:
    percent = cell.percent if cell is not None else None
    return 0.0 if percent is None else percent


def _percent_text(cell: PriceCell | None) -> str:
    # «+0.0 %» у каждой неизменной цены в нескольких колонках — только шум:
    # процент виден там, где цена правда меняется.
    if cell is None or not cell.changed or cell.percent is None:
        return ""
    return f"{cell.percent:+.1f} %"


def _percent_color(cell: PriceCell | None) -> QColor | None:
    percent = cell.percent if cell is not None else None
    if percent is None or abs(percent) < 0.05:
        return None
    return QColor(Palette.DANGER if percent > 0 else Palette.SUCCESS)


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".00", "")


def _select_sheet(picker: FilePicker, name: str) -> None:
    box = picker.sheet_box
    if name and box.currentText() != name:
        box.blockSignals(True)
        box.setCurrentText(name)
        box.blockSignals(False)


def _write_missing(lines: list[PriceLine], path: str) -> None:
    """Позиции без уверенного сопоставления — чтобы запросить их у поставщика."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Не сопоставлено"
    sheet.append(["Строка 1С", "Артикул", "Товар", "Статус", "Возможные варианты"])
    for line in lines:
        variants = "; ".join(
            f"{c.record.text(FieldRole.ARTICLE)} ({c.score:.0f})"
            for c in pricing.choices(line, 5))
        sheet.append([line.row, line.article, line.name, line.status.title, variants])
    for column, width in zip("ABCDE", (12, 26, 58, 24, 52)):
        sheet.column_dimensions[column].width = width
    book.save(path)


