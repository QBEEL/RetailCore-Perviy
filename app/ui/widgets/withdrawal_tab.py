"""Вкладка «Вывод из оборота»: сканируем марки, выбираем причину, отправляем документ.

Работа та же, что при сверке: вещь в руках, следующая в коробке, и ответ на скан
должен быть виден раньше, чем прочитан, — зелёное «принято», жёлтое «уже в
списке», красное «не разобран». Но итог другой: не сверка, а документ, который
выводит коды из оборота. Он необратим, поэтому отправка всегда спрашивает
подтверждение, а оборванный ответ не повторяется (общий разговор с вводом в
оборот — `introduce.send`).

Что именно уходит, видно до отправки: причина, число кодов и контур. Каждая
отправка записывается в журнал операций вкладки «Проверка кодов», а такой же
набор кодов, который уже отправляли, узнаётся и называется до подтверждения —
чаще всего это второе нажатие, а не второй случай.

Формат документа и список причин — из ПРИНТМАРКИ, см. `core.marking.withdrawal`.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Callable

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.marking import codes as codes_module, introduce, service, withdrawal
from ...core.marking.models import (
    GROUPS,
    Contour,
    Operation,
    OperationKind,
    OperationStatus,
)
from ...core.marking.reconcile import key_of
from ...core.marking.session import Organisation
from ...core.settings import AppSettings
from .. import icons
from ..tasks import run_task
from ..theme import Metrics, Palette
from .common import Card, Hint, MetricTile, SectionTitle
from .toast import ToastKind

# Группа по умолчанию — лёгкая промышленность: ею здесь торгуют чаще всего, а
# вывод без группы система не принимает (`?pg=`).
DEFAULT_GROUP = "lp"

# Колонки таблицы кодов: наименование идёт сразу за кодом, цена — последняя.
GTIN_COLUMN = 1
NAME_COLUMN = 4
PRICE_COLUMN = 5
EDITABLE_COLUMNS = (NAME_COLUMN, PRICE_COLUMN)


class _CellEditor(QStyledItemDelegate):
    """Редактор ячейки по размеру строки.

    Стандартный берёт отступы поля формы, в строку таблицы они не помещаются, и
    набираемый текст — цену — не видно. Так же устроена таблица во вкладке «Продажа».
    """

    def createEditor(self, parent, option, index):  # noqa: N802 — Qt
        editor = QLineEdit(parent)
        editor.setStyleSheet("QLineEdit { padding: 0 7px; margin: 0; border-radius: 0; }")
        if index.column() == PRICE_COLUMN:
            editor.setPlaceholderText("0,00")
            editor.setAlignment(Qt.AlignmentFlag.AlignRight
                                | Qt.AlignmentFlag.AlignVCenter)
        return editor

    def updateEditorGeometry(self, editor, option, index):  # noqa: N802 — Qt
        editor.setGeometry(option.rect)

# Цвет и фон ответа на скан: зелёное «принято», жёлтое «повтор», красное «не принято».
RESULT_COLOURS = {
    "added": (Palette.SUCCESS, Palette.SUCCESS_SOFT),
    "repeat": (Palette.WARNING, Palette.WARNING_SOFT),
    "broken": (Palette.DANGER, Palette.DANGER_SOFT),
}


class WithdrawalTab(QWidget):
    """Список кодов, реквизиты документа и отправка в «Честный ЗНАК»."""

    # Журнал операций на соседней вкладке читает базу, и после каждой отправки
    # ему нужно об этом сказать.
    journal_changed = Signal()

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, object], None] | None = None,
        parent: QWidget | None = None,
        *,
        contour: Callable[[], Contour] = lambda: Contour.SANDBOX,
        thumbprint: Callable[[], str] = lambda: "",
        inn: Callable[[], str] = lambda: "",
        organisations: Callable[[], list[Organisation]] = lambda: [],
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._contour = contour
        self._thumbprint = thumbprint
        self._inn = inn
        self._organisations = organisations
        # ИНН, который подставили мы: пока в поле он, его можно менять на ИНН
        # вошедшей организации; набранное руками не трогаем.
        self._filled_inn = ""
        self._refreshing = False
        # Наименования по коду товара (GTIN): их знает память «Продажи», кэш
        # проверок и, если вошли, «Честный ЗНАК». `_asked` — по кому уже спросили.
        self._names: dict[str, str] = {}
        self._asked: set[str] = set()
        self._finding = False
        # Коды как отсканированы — документ сам возьмёт из них код идентификации.
        self.codes: list[str] = []
        self._keys: set[str] = set()
        self._busy = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, Metrics.GAP, 0, 0)
        root.setSpacing(Metrics.GAP)
        root.addWidget(self._details_card())
        root.addWidget(self._codes_card(), 1)
        self._sync()

    # --- построение ----------------------------------------------------------------

    def _details_card(self) -> Card:
        card = Card(self)
        body = card.body()
        body.addWidget(SectionTitle("Реквизиты документа", card))

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(9)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        # ИНН — список знакомых организаций с возможностью вписать свой: по
        # умолчанию стоит ИНН организации, под которой выполнен вход.
        self.inn_box = QComboBox(card)
        self.inn_box.setEditable(True)
        self.inn_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.inn_box.setCompleter(None)
        self.inn_box.activated.connect(self._pick_organisation)
        self.inn_edit = self.inn_box.lineEdit()
        self.inn_edit.setPlaceholderText("ИНН организации или ИП")
        self.inn_edit.setMaxLength(12)
        self.inn_box.setToolTip(
            "ИНН участника оборота. Подставляется из организации, под которой "
            "выполнен вход; другую можно выбрать в списке или вписать.")
        self.date_edit = QDateEdit(QDate.currentDate(), card)
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd.MM.yyyy")
        # Место осуществления деятельности: система ищет его по ИНН, КПП и ФИАС и
        # без них отклоняет документ. Один раз вписанное помнится по ИНН.
        self.fias_edit = QLineEdit(card)
        self.fias_edit.setPlaceholderText("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        self.fias_edit.editingFinished.connect(self._tidy_fias)
        self.fias_edit.setToolTip(
            "Идентификатор (ФИАС, ГАР) адреса места осуществления деятельности — "
            "из личного кабинета «Честного ЗНАКа», раздел «Места осуществления "
            "деятельности». Запоминается для этого ИНН.")
        self.kpp_edit = QLineEdit(card)
        self.kpp_edit.setPlaceholderText("9 цифр; у ИП не нужен")
        self.kpp_edit.setMaxLength(9)
        self.inn_edit.editingFinished.connect(self.recall_place)
        self.group_box = QComboBox(card)
        for group in GROUPS:
            self.group_box.addItem(group.title, group.code)
        self.group_box.setCurrentIndex(max(0, self.group_box.findData(DEFAULT_GROUP)))
        self.group_box.setToolTip(
            "Товарная группа кодов в списке: документ создаётся по одной группе")

        self.cause_box = QComboBox(card)
        for code in withdrawal.WITHDRAWAL_CAUSES:
            self.cause_box.addItem(withdrawal.cause_title(code), code)
        self.cause_box.setCurrentIndex(
            self.cause_box.findData(withdrawal.DEFAULT_CAUSE))
        self.cause_box.currentIndexChanged.connect(self._sync)
        self.cause_other_edit = QLineEdit(card)
        self.cause_other_edit.setPlaceholderText("Опишите причину своими словами")

        self.doc_type_box = QComboBox(card)
        self.doc_type_box.addItem("— не указывать —", "")
        for code, title in withdrawal.PRIMARY_DOCUMENT_TYPES.items():
            self.doc_type_box.addItem(f"{title} — {code}", code)
        self.doc_type_box.setCurrentIndex(
            self.doc_type_box.findData(withdrawal.DEFAULT_DOCUMENT_TYPE))
        self.doc_type_box.currentIndexChanged.connect(self._sync)
        self.doc_number_edit = QLineEdit(card)
        self.doc_number_edit.setPlaceholderText("Номер чека или документа")
        self.doc_date_edit = QDateEdit(QDate.currentDate(), card)
        self.doc_date_edit.setCalendarPopup(True)
        self.doc_date_edit.setDisplayFormat("dd.MM.yyyy")
        self.doc_name_edit = QLineEdit(card)
        self.doc_name_edit.setPlaceholderText("Наименование иного документа")

        self.cause_other_label = QLabel("Описание причины", card)
        self.doc_name_label = QLabel("Наименование документа", card)
        rows = (
            (QLabel("ИНН участника", card), self.inn_box,
             QLabel("Дата вывода", card), self.date_edit),
            (QLabel("Причина вывода", card), self.cause_box,
             QLabel("Товарная группа", card), self.group_box),
        )
        row = 0
        for first_label, first, second_label, second in rows:
            grid.addWidget(first_label, row, 0)
            grid.addWidget(first, row, 1)
            grid.addWidget(second_label, row, 2)
            grid.addWidget(second, row, 3)
            row += 1
        grid.addWidget(QLabel("ФИАС места деятельности", card), row, 0)
        grid.addWidget(self.fias_edit, row, 1)
        grid.addWidget(QLabel("КПП места деятельности", card), row, 2)
        grid.addWidget(self.kpp_edit, row, 3)
        row += 1
        grid.addWidget(self.cause_other_label, row, 0)
        grid.addWidget(self.cause_other_edit, row, 1, 1, 3)
        row += 1
        grid.addWidget(QLabel("Первичный документ", card), row, 0)
        grid.addWidget(self.doc_type_box, row, 1)
        self.doc_number_label = QLabel("Номер", card)
        grid.addWidget(self.doc_number_label, row, 2)
        grid.addWidget(self.doc_number_edit, row, 3)
        row += 1
        self.doc_date_label = QLabel("Дата документа", card)
        grid.addWidget(self.doc_date_label, row, 0)
        grid.addWidget(self.doc_date_edit, row, 1)
        grid.addWidget(self.doc_name_label, row, 2)
        grid.addWidget(self.doc_name_edit, row, 3)
        # Названия причин длинные («…для производственных целей — PRODUCTION_USE»),
        # и список, который не умеет сжиматься, раздувал вкладку шире окна.
        for combo in (self.inn_box, self.group_box, self.cause_box,
                      self.doc_type_box):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
        body.addLayout(grid)

        self.details_hint = Hint(
            "При продаже, фасовке и передаче у каждого товара нужна цена за "
            "единицу: впишите её в таблице ниже, в колонке «Цена, ₽». Чек или иной "
            "документ нужен не для каждой причины: какой — решает система, и "
            "отказ она объяснит. Номера ККТ в формате документа нет. ФИАС и КПП — "
            "адрес вашего места осуществления деятельности из личного кабинета.",
            card)
        body.addWidget(self.details_hint)
        return card

    def _codes_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Коды к выводу", card))
        header.addStretch(1)
        self.paste_button = self._button(card, "Вставить", "copy",
                                         self.paste_codes)
        self.file_button = self._button(card, "Из файла", "open", self.load_file)
        self.remove_button = self._button(card, "Убрать", "trash",
                                          self.remove_selected)
        self.clear_button = self._button(card, "Очистить", "clear", self.clear)
        self.paste_button.setToolTip("Вставить коды из буфера обмена — по одному в строке")
        self.file_button.setToolTip("Взять коды из текстового файла — по одному в строке")
        self.remove_button.setToolTip("Убрать из списка выбранные в таблице коды")
        self.clear_button.setToolTip("Очистить весь список")
        for button in (self.paste_button, self.file_button, self.remove_button,
                       self.clear_button):
            header.addWidget(button)
        body.addLayout(header)

        self.scan_edit = QLineEdit(card)
        self.scan_edit.setPlaceholderText(
            "Наведите сканер на марку — код придёт сюда сам")
        self.scan_edit.setMinimumHeight(38)
        self.scan_edit.returnPressed.connect(self.accept_scan)
        body.addWidget(self.scan_edit)

        self.verdict_label = QLabel("", card)
        self.verdict_label.setWordWrap(True)
        self.verdict_label.setMinimumHeight(46)
        self.verdict_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        # Пустая плашка места не занимает: она появляется с первым сканом.
        self.verdict_label.hide()
        body.addWidget(self.verdict_label)

        tiles = QHBoxLayout()
        tiles.setSpacing(Metrics.GAP)
        self.tile_codes = MetricTile("Кодов в списке", Palette.PRIMARY, card)
        self.tile_repeats = MetricTile("Повторов", Palette.WARNING, card)
        self.tile_broken = MetricTile("Не принято", Palette.DANGER, card)
        for tile in (self.tile_codes, self.tile_repeats, self.tile_broken):
            tiles.addWidget(tile, 1)
        body.addLayout(tiles)
        self._repeats = 0
        self._broken = 0

        self.sound_box = QCheckBox("Звук при повторе и ошибке", card)
        self.sound_box.setChecked(self.settings.marking_reconcile_sound)
        body.addWidget(self.sound_box)

        self.table = QTableWidget(0, 6, card)
        self.table.setHorizontalHeaderLabels(
            ["№", "Код товара", "Серийный номер", "Код", "Наименование",
             "Цена за единицу, ₽"])
        self.table.setItemDelegate(_CellEditor(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Правятся наименование и цена; остальное в таблице только для чтения.
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setMinimumHeight(140)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(NAME_COLUMN, QHeaderView.ResizeMode.Stretch)
        for column in (0, 1, 2, 3, PRICE_COLUMN):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._sync)
        self.table.itemChanged.connect(self._cell_edited)
        body.addWidget(self.table, 1)

        actions = QHBoxLayout()
        actions.setSpacing(9)
        self.hint = Hint("", card)
        actions.addWidget(self.hint, 1)
        self.file_save_button = self._button(card, "Сохранить JSON…", "save",
                                             self.save_file)
        self.send_button = self._button(card, "Создать документ в ЧЗ", "export",
                                        self.create_document)
        self.send_button.setObjectName("Primary")
        actions.addWidget(self.file_save_button)
        actions.addWidget(self.send_button)
        body.addLayout(actions)
        return card

    @staticmethod
    def _button(parent: QWidget, text: str, icon: str,
                handler: Callable[[], None]) -> QPushButton:
        button = QPushButton(text, parent)
        button.setIcon(icons.icon(icon))
        button.clicked.connect(lambda _checked=False: handler())
        return button

    # --- скан ----------------------------------------------------------------------

    def focus_scan(self) -> None:
        """Курсор в поле скана. Без этого сканер печатает мимо."""
        self.scan_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def accept_scan(self) -> None:
        """Принимает один код из поля сканирования.

        Поле очищается всегда: оставленный в нём код сканер допишет своим, и
        следующая марка окажется «не разобрана» по нашей вине.
        """
        text = self.scan_edit.text().strip()
        self.scan_edit.clear()
        if text:
            self.add_code(text)

    def add_code(self, text: str, *, quiet: bool = False) -> str:
        """Добавляет код в список. Возвращает `added`, `repeat` или `broken`."""
        text = codes_module.from_keyboard((text or "").strip())
        parsed = codes_module.parse(text)
        if not text or not parsed.valid or not parsed.ki:
            self._broken += 1
            reason = parsed.problems[0] if parsed.problems else "код не разобран"
            if not quiet:
                self._say("broken", f"Не принято: {reason}")
            self._sync()
            return "broken"
        key = key_of(text)
        if key in self._keys:
            self._repeats += 1
            if not quiet:
                self._say("repeat", f"Уже в списке · {parsed.gtin} · {parsed.serial}")
            self._sync()
            return "repeat"
        self._keys.add(key)
        self.codes.append(text)
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._refreshing = True
        try:
            for column, value in enumerate((str(row + 1), parsed.gtin, parsed.serial,
                                            parsed.ki)):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, column, item)
            self.table.setItem(row, NAME_COLUMN,
                               QTableWidgetItem(self._name_of(parsed.gtin)))
            price = QTableWidgetItem(self._price_known(parsed.gtin))
            price.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                   | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, PRICE_COLUMN, price)
        finally:
            self._refreshing = False
        self._paint_prices()
        if not quiet:
            self._say("added", f"Принято · {parsed.gtin} · {parsed.serial}")
            self.table.scrollToBottom()
        self._sync()
        self._find_names()
        return "added"

    def _say(self, kind: str, text: str) -> None:
        """Ответ на скан: цвет во всю ширину, а при замечании — ещё и звук."""
        colour, background = RESULT_COLOURS[kind]
        self.verdict_label.setText(text)
        self.verdict_label.show()
        self.verdict_label.setStyleSheet(
            f"color: {colour}; background: {background}; font-size: 18px;"
            f" font-weight: 700; border-radius: {Metrics.RADIUS}px;"
            " padding: 8px 14px;")
        if kind != "added" and self.sound_box.isChecked():
            QApplication.beep()

    def paste_codes(self) -> None:
        self.add_many(QApplication.clipboard().text())

    def load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Файл с кодами", "", "Текст (*.txt *.csv);;Все файлы (*.*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as handle:
                text = handle.read()
        except OSError as error:
            self._tell(f"Не удалось прочитать файл: {error}", ToastKind.ERROR)
            return
        self.add_many(text)

    def add_many(self, text: str) -> tuple[int, int, int]:
        """Добавляет коды списком и говорит итог одной строкой."""
        added = repeats = broken = 0
        for line in codes_module.split_lines(text):
            result = self.add_code(line, quiet=True)
            added += result == "added"
            repeats += result == "repeat"
            broken += result == "broken"
        if added or repeats or broken:
            tail = []
            if repeats:
                tail.append(f"повторов {repeats}")
            if broken:
                tail.append(f"не принято {broken}")
            self._say("added" if added else "broken",
                      f"Добавлено кодов: {added}"
                      + (" · " + ", ".join(tail) if tail else ""))
        return added, repeats, broken

    def remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectionModel()
                       .selectedRows()}, reverse=True)
        for row in rows:
            self._keys.discard(key_of(self.codes[row]))
            del self.codes[row]
            self.table.removeRow(row)
        self._renumber()
        self._sync()

    def clear(self) -> None:
        self.codes.clear()
        self._keys.clear()
        self.table.setRowCount(0)
        self._repeats = self._broken = 0
        self.verdict_label.setText("")
        self.verdict_label.setStyleSheet("")
        self.verdict_label.hide()
        self._sync()
        self.focus_scan()

    def _renumber(self) -> None:
        for row in range(self.table.rowCount()):
            item = QTableWidgetItem(str(row + 1))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, item)

    # --- цена и наименование -------------------------------------------------------

    def _gtin_of(self, row: int) -> str:
        item = self.table.item(row, GTIN_COLUMN)
        return item.text() if item is not None else ""

    def _price_known(self, gtin: str) -> str:
        """Цена, уже вписанная у другого кода этого товара, — чтобы не набирать её заново."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, PRICE_COLUMN)
            if self._gtin_of(row) == gtin and item is not None and item.text().strip():
                return item.text().strip()
        return ""

    def _cell_edited(self, item: QTableWidgetItem) -> None:
        """Вписали наименование или цену: то же — остальным кодам этого товара."""
        if self._refreshing or item.column() not in EDITABLE_COLUMNS:
            return
        gtin, text = self._gtin_of(item.row()), item.text().strip()
        self._refreshing = True
        try:
            for row in range(self.table.rowCount()):
                other = self.table.item(row, item.column())
                if other is None or row == item.row() or self._gtin_of(row) != gtin:
                    continue
                if item.column() == NAME_COLUMN:
                    other.setText(text)
                elif not other.text().strip():
                    # Цена подставляется только туда, где её ещё нет.
                    other.setText(text)
        finally:
            self._refreshing = False
        if item.column() == NAME_COLUMN:
            self._names[gtin] = text
        self._paint_prices()

    def _paint_prices(self) -> None:
        """Подсвечивает строки, где цена нужна, а её нет, — чтобы было видно, где вписать."""
        priced = self.cause_box.currentData() in withdrawal.COST_CAUSES
        warning = QBrush(QColor(Palette.WARNING_SOFT))
        self._refreshing = True
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, PRICE_COLUMN)
                if item is not None:
                    item.setBackground(
                        warning if priced and not item.text().strip() else QBrush())
        finally:
            self._refreshing = False

    def _row_prices(self) -> tuple[str, ...]:
        prices = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, PRICE_COLUMN)
            prices.append(item.text().strip() if item is not None else "")
        return tuple(prices)

    def _name_of(self, gtin: str) -> str:
        if gtin not in self._names:
            known = self.settings.marking_sale_items.get(gtin, {})
            self._names[gtin] = known.get("name", "")
        return self._names[gtin]

    def _find_names(self) -> None:
        """Наименования, которых ещё нет, ищутся в кэше и у «Честного ЗНАКа».

        Фоном и по одному коду на товар: список не ждёт, а нет ответа — ячейка
        остаётся пустой, и название можно вписать руками.
        """
        if self._finding:
            return
        wanted: dict[str, str] = {}
        for row, code in enumerate(self.codes):
            gtin = self._gtin_of(row)
            if gtin and not self._names.get(gtin) and gtin not in self._asked:
                wanted.setdefault(gtin, code)
        if not wanted:
            return
        self._asked.update(wanted)
        self._finding = True
        online = service.signed_in()
        run_task(
            _look_up_names, list(wanted.values()), self._contour(), online,
            on_result=self._names_found,
            on_error=lambda _message: self._names_found({}))

    def _names_found(self, found: dict[str, str]) -> None:
        self._finding = False
        self._refreshing = True
        try:
            for gtin, name in found.items():
                if not name or self._names.get(gtin):
                    continue
                self._names[gtin] = name
                for row in range(self.table.rowCount()):
                    item = self.table.item(row, NAME_COLUMN)
                    if self._gtin_of(row) == gtin and item is not None \
                            and not item.text().strip():
                        item.setText(name)
        finally:
            self._refreshing = False
        self._find_names()

    # --- документ ------------------------------------------------------------------

    def document(self) -> withdrawal.Withdrawal:
        """Документ из того, что сейчас на экране. Проверяется отдельно: `problems`."""
        cause = self.cause_box.currentData() or ""
        doc_type = self.doc_type_box.currentData() or ""
        return withdrawal.Withdrawal(
            inn=self.inn_edit.text().strip(),
            cause=cause,
            date=self.date_edit.date().toString("yyyy-MM-dd"),
            codes=tuple(self.codes),
            cause_other=self.cause_other_edit.text() if cause == "OTHER" else "",
            doc_type=doc_type,
            doc_number=self.doc_number_edit.text() if doc_type else "",
            doc_date=(self.doc_date_edit.date().toString("yyyy-MM-dd")
                      if doc_type else ""),
            doc_name=self.doc_name_edit.text() if doc_type == "OTHER" else "",
            fias_id=self.fias_edit.text().strip(),
            kpp=self.kpp_edit.text().strip(),
            code_prices=self._row_prices())

    @property
    def group(self) -> str:
        return str(self.group_box.currentData() or "")

    def prefill_inn(self) -> None:
        """Ставит ИНН организации, под которой выполнен вход, и обновляет список.

        Вписанное руками не трогается: заменяется только пустое поле или ИНН,
        подставленный ранее (вошли под другой организацией).
        """
        self._load_organisations()
        current = (self._inn() or "").strip()
        shown = self.inn_edit.text().strip()
        if current and (not shown or shown == self._filled_inn):
            self.inn_edit.setText(current)
            self._filled_inn = current
        self.recall_place()

    def _tidy_fias(self) -> None:
        """Вставленный с мусором идентификатор приводится к чистому виду."""
        if tidy := withdrawal.fias(self.fias_edit.text()):
            self.fias_edit.setText(tidy)

    def recall_place(self) -> None:
        """Подставляет запомненные ФИАС и КПП этого ИНН, если поля ещё пусты."""
        known = self.settings.marking_places.get(self.inn_edit.text().strip(), {})
        for edit, name in ((self.fias_edit, "fias_id"), (self.kpp_edit, "kpp")):
            if not edit.text().strip():
                edit.setText(known.get(name, ""))

    def _remember_place(self, document: withdrawal.Withdrawal) -> None:
        """Документ отправлен: адрес и КПП запоминаются, чтобы не вписывать их снова."""
        self.settings.marking_places[document.inn.strip()] = {
            "fias_id": withdrawal.fias(document.fias_id), "kpp": document.kpp.strip()}
        self.settings.save()

    def _load_organisations(self) -> None:
        """Список знакомых организаций: запомненные при входе и вошедшая."""
        known = list(self._organisations() or [])
        if (current := (self._inn() or "").strip()) and all(
                item.inn != current for item in known):
            known.insert(0, Organisation(inn=current))
        typed = self.inn_edit.text()
        self.inn_box.blockSignals(True)
        self.inn_box.clear()
        for item in known:
            self.inn_box.addItem(item.title, item.inn)
        self.inn_box.setCurrentIndex(-1)
        self.inn_edit.setText(typed)
        self.inn_box.blockSignals(False)

    def _pick_organisation(self, index: int) -> None:
        """В списке выбрали организацию: в поле — её ИНН, а не подпись с названием."""
        if inn := self.inn_box.itemData(index):
            self.inn_edit.setText(str(inn))
            # Другая организация — другой адрес: чужой не оставляем.
            self.fias_edit.clear()
            self.kpp_edit.clear()
            self.recall_place()

    def save_file(self) -> None:
        """Сохраняет JSON для загрузки в личном кабинете. В систему ничего не уходит."""
        document = self.document()
        if problems := document.problems:
            self._tell(f"Файл не собран: {problems[0]}", ToastKind.WARNING)
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить документ вывода из оборота",
            withdrawal.file_name(), document.file_filters)
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".json"
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(document.render(".json"))
        except (OSError, introduce.IntroduceProblem) as error:
            self._tell(f"Не удалось сохранить файл: {error}", ToastKind.ERROR)
            return
        self._tell(f"Файл сохранён: {path}. Загрузите его в личном кабинете "
                   "системы маркировки.", ToastKind.SUCCESS)

    def create_document(self) -> None:
        """Подписывает документ и отправляет его — необратимо, поэтому с вопросом."""
        document = self.document()
        if problems := document.problems:
            self._tell(f"Документ не отправлен: {problems[0]}", ToastKind.WARNING)
            return
        if not self._thumbprint():
            self._tell("Документ подписывается — выберите сертификат на вкладке "
                       "«Проверка кодов»", ToastKind.WARNING)
            return
        if not service.signed_in():
            self._tell("Отправка идёт от имени организации — войдите по "
                       "сертификату на вкладке «Проверка кодов»", ToastKind.WARNING)
            return
        contour = self._contour()
        if service.current().contour != contour:
            self._tell("Вход выполнен в другом контуре — войдите заново",
                       ToastKind.WARNING)
            return
        identifiers = [codes_module.parse(code).ki for code in document.codes]
        earlier = service.warn_duplicates(
            Operation(kind=OperationKind.WITHDRAWAL, codes=identifiers))
        if not self._confirm(document, contour, earlier):
            return

        operation = service.start(
            OperationKind.WITHDRAWAL, identifiers, group=self.group,
            contour=contour, reason=document.cause,
            comment=withdrawal.cause_title(document.cause))
        operation.status = OperationStatus.SENDING
        operation.sent_at = datetime.now()
        service.store.update(operation)
        self.journal_changed.emit()

        self._busy = True
        self._sync()
        self.hint.setStyleSheet("")
        self.hint.setText("Подписываем и отправляем документ — КриптоПро может "
                          "спросить пароль…")

        def created(doc_id: str) -> None:
            # Номер документа — единственное, по чему потом узнают его судьбу.
            operation.document_id = doc_id
            operation.status = OperationStatus.SENT
            service.store.update(operation)

        run_task(
            _send, document, self._thumbprint(), self.group, contour, created,
            on_result=lambda outcome: self._on_sent(operation, document, outcome),
            on_error=lambda message: self._on_send_error(operation, message))

    def _confirm(self, document: withdrawal.Withdrawal, contour: Contour,
                 earlier: list[Operation]) -> bool:
        lines = [f"Причина: {withdrawal.cause_title(document.cause)}.",
                 f"Дата вывода: {self.date_edit.date().toString('dd.MM.yyyy')}.",
                 f"Кодов: {len(document.codes)}. Контур: {contour.title}."]
        if earlier:
            when = earlier[0].created_at
            stamp = f" {when:%d.%m.%Y %H:%M}" if when else ""
            lines.append(f"\nВНИМАНИЕ: такой же набор кодов уже отправляли{stamp} "
                         f"({earlier[0].status.title.lower()}). Скорее всего, это "
                         "второе нажатие.")
        lines.append("\nДокумент подписывается сертификатом и уходит в «Честный "
                     "ЗНАК». Вернуть коды в оборот этим же документом нельзя.")
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Вывод из оборота")
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setText(f"Вывести из оборота {len(document.codes)} кодов?")
        confirm.setInformativeText("\n".join(lines))
        yes = confirm.addButton("Вывести из оборота",
                                QMessageBox.ButtonRole.DestructiveRole)
        cancel = confirm.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        confirm.setDefaultButton(cancel)
        confirm.exec()
        return confirm.clickedButton() is yes

    def _on_sent(self, operation: Operation, document: withdrawal.Withdrawal,
                 outcome: tuple) -> None:
        kind, payload = outcome
        self._busy = False
        if kind == "unknown":
            # Документ мог дойти: операция остаётся «ждёт ответа», а список кодов —
            # на месте, чтобы его не пришлось собирать заново.
            operation.status = OperationStatus.SENT
            operation.error = "исход неизвестен"
            service.store.update(operation)
            self.journal_changed.emit()
            self._sync()
            self.hint.setStyleSheet(f"color: {Palette.WARNING};")
            self.hint.setText(payload)
            self._tell(payload, ToastKind.WARNING)
            return
        sent: introduce.Sent = payload
        status = sent.status
        if not status.failed:
            self._remember_place(document)
        operation.document_id = sent.doc_id
        if status.done:
            operation.status = OperationStatus.DONE
            operation.checked_at = datetime.now()
        elif status.failed:
            operation.status = OperationStatus.REJECTED
            operation.error = status.text
        else:
            operation.status = OperationStatus.SENT
        service.store.update(operation)
        self.journal_changed.emit()
        if status.done or not status.failed:
            # Документ принят или ждёт обработки: коды уже ушли, и оставлять их
            # в списке значило бы подставить их под второй документ.
            self.clear()
        self._sync()
        if status.done:
            text = f"Выведено из оборота: {len(document.codes)} кодов. Документ {sent.doc_id}"
            self.hint.setStyleSheet("")
            self._tell(text, ToastKind.SUCCESS)
        elif status.failed:
            text = f"Документ {sent.doc_id}: {status.text}"
            self.hint.setStyleSheet(f"color: {Palette.DANGER};")
            self._tell(f"Вывод не выполнен. {text}", ToastKind.ERROR)
        else:
            text = (f"Документ {sent.doc_id} создан, итога пока нет: {status.text}. "
                    "Состояние смотрите в личном кабинете «Честного ЗНАКа».")
            self.hint.setStyleSheet(f"color: {Palette.WARNING};")
            self._tell(text, ToastKind.WARNING)
        self.hint.setText(text)

    def _on_send_error(self, operation: Operation, message: str) -> None:
        """Отправка не состоялась: подпись, вход, проверка — до самого запроса."""
        self._busy = False
        operation.status = OperationStatus.FAILED
        operation.error = message
        service.store.update(operation)
        self.journal_changed.emit()
        self._sync()
        self.hint.setStyleSheet(f"color: {Palette.DANGER};")
        self.hint.setText(f"Документ не отправлен: {message}")
        self._tell(f"Документ не отправлен: {message}", ToastKind.ERROR)

    # --- состояние -----------------------------------------------------------------

    def _tell(self, text: str, kind: ToastKind) -> None:
        if self.notify:
            self.notify(text, kind)

    def _sync(self) -> None:
        """Приводит поля, плитки и кнопки к тому, что выбрано и набрано."""
        priced = self.cause_box.currentData() in withdrawal.COST_CAUSES
        self.table.setColumnHidden(PRICE_COLUMN, not priced)
        self._paint_prices()
        other = self.cause_box.currentData() == "OTHER"
        self.cause_other_label.setVisible(other)
        self.cause_other_edit.setVisible(other)
        doc_type = self.doc_type_box.currentData() or ""
        for widget in (self.doc_number_label, self.doc_number_edit,
                       self.doc_date_label, self.doc_date_edit):
            widget.setEnabled(bool(doc_type))
        named = doc_type == "OTHER"
        self.doc_name_label.setVisible(named)
        self.doc_name_edit.setVisible(named)

        self.tile_codes.set_value(len(self.codes))
        self.tile_repeats.set_value(self._repeats)
        self.tile_broken.set_value(self._broken)
        has_codes = bool(self.codes)
        self.remove_button.setEnabled(
            bool(self.table.selectionModel() and self.table.selectionModel().hasSelection())
            and not self._busy)
        self.clear_button.setEnabled(has_codes and not self._busy)
        self.file_save_button.setEnabled(has_codes and not self._busy)
        self.send_button.setEnabled(has_codes and not self._busy)
        for widget in (self.scan_edit, self.paste_button, self.file_button):
            widget.setEnabled(not self._busy)
        if not self._busy and not self.hint.text():
            self.hint.setText(
                "Отсканируйте марки и нажмите «Создать документ в ЧЗ»: вывод из "
                "оборота необратим, поэтому перед отправкой будет вопрос.")


def _send(document: withdrawal.Withdrawal, thumbprint: str, group: str,
          contour: Contour, created: Callable[[str], None]) -> tuple[str, object]:
    """Отправка в фоне. Неизвестный исход возвращается значением, а не ошибкой.

    Ошибка и «неизвестно» лечатся по-разному: после ошибки список кодов можно
    отправлять снова, а после неизвестного исхода — нельзя, пока не посмотрели
    личный кабинет. По тексту исключения их не различить, поэтому различаются
    здесь, пока тип ещё известен.
    """
    try:
        return "sent", introduce.send(document, thumbprint, group, contour,
                                      on_created=created)
    except introduce.IntroduceUnknown as error:
        return "unknown", str(error)


def _look_up_names(codes: list[str], contour: Contour,
                online: bool) -> dict[str, str]:
    """Наименования по одному коду на товар: сначала кэш проверок, потом система.

    Без входа спрашивать некого — остаётся кэш. Возвращается {GTIN: наименование}.
    """
    requested = {code: codes_module.for_request(code) or code for code in codes}
    found: dict[str, str] = {}
    for info in service.store.known_checks(
            list(requested.values()), contour).values():
        if info.product_name:
            found[info.gtin or codes_module.parse(info.code).gtin] = info.product_name
    missing = [code for code in codes
               if codes_module.parse(code).gtin not in found]
    if online and missing:
        for info in service.check(missing, contour=contour):
            gtin = info.gtin or codes_module.parse(info.code).gtin
            if gtin and info.product_name:
                found.setdefault(gtin, info.product_name)
    return found
