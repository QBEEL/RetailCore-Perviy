"""Вкладка «Продажа»: УПД покупателю-организации с выводом кодов из оборота.

Сценарий — продажа организации для её собственных нужд (не для перепродажи):
отсканировать товар, указать покупателя и цены, проверить коды в «Честном
ЗНАКе» и отправить УПД покупателю через «ЭДО Лайт». В УПД стоит признак вывода
из оборота, и, когда покупатель подпишет документ, коды выбудут сами — отдельный
документ «Вывод из оборота» не нужен (см. `core.marking.sale`).

Порядок отправки выбран так, чтобы отказ случался до документа, а не после:

1. документ проверяется на месте — реквизиты, цены, наименования;
2. коды спрашиваются у «Честного ЗНАКа»: продать можно только свой код в
   обороте, а чужой выяснился бы лишь после подписи покупателя;
3. подтверждение с покупателем, суммой и числом кодов;
4. подпись и отправка. Оборванный ответ лечится повторной отправкой того же
   документа: оператор узнаёт файл по имени и второго не создаёт.

Отправленные УПД видны внизу, их статус спрашивается у оператора кнопкой
«Обновить статусы». Окончательный ответ — в самих кодах: «выведены из оборота»
пишется, только когда «Честный ЗНАК» подтвердил это по каждому коду.
"""
from __future__ import annotations

import os
import re
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Callable

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QColor, QIntValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
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

from ...core.marking import codes as codes_module, crypto, edo, sale, service, transport
from ...core.marking.models import CodeInfo, Contour, Operation, OperationKind, OperationStatus
from ...core.marking.reconcile import key_of
from ...core.settings import AppSettings
from .. import icons
from ..tasks import run_task
from ..theme import Metrics, Palette
from .common import Card, Hint, SectionTitle
from .toast import ToastKind

# Колонки таблицы товаров. Править можно то, чего не знает ни сканер, ни
# «Честный ЗНАК»: название (подставляется из ответа), цену, страну и декларацию.
# Количество правится только у товара без марок — у маркированного это число кодов.
COLUMNS = ("GTIN", "Наименование", "Шт.", "Цена с НДС", "Сумма", "Страна",
           "Номер ДТ", "Честный ЗНАК")
NAME, QUANTITY, PRICE, SUM, ORIGIN, CUSTOMS, CHECK = range(1, 8)
EDITABLE = (NAME, QUANTITY, PRICE, ORIGIN, CUSTOMS)
# Строка товара без марок, добавленная руками, — у неё нет GTIN, и узнаётся она
# по своему ключу.
MANUAL_KEY = "#"
UNMARKED_TEXT = "без марок"

# Сколько отправленных УПД показывать внизу.
SENT_LIMIT = 20

RESULT_COLOURS = {
    "added": (Palette.SUCCESS, Palette.SUCCESS_SOFT),
    "repeat": (Palette.WARNING, Palette.WARNING_SOFT),
    "broken": (Palette.DANGER, Palette.DANGER_SOFT),
}


class _CellEditor(QStyledItemDelegate):
    """Редактор ячейки по размеру строки.

    Стандартный брал отступы поля формы, в строку таблицы они не помещались, и
    текст при правке обрезался сверху и снизу.
    """

    def createEditor(self, parent, option, index):  # noqa: N802 — Qt
        editor = QLineEdit(parent)
        editor.setStyleSheet("QLineEdit { padding: 0 7px; margin: 0; border-radius: 0; }")
        if index.column() == QUANTITY:
            editor.setValidator(QIntValidator(1, 999999, editor))
        if index.column() in (QUANTITY, PRICE):
            editor.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return editor

    def updateEditorGeometry(self, editor, option, index):  # noqa: N802 — Qt
        editor.setGeometry(option.rect)


def rubles(value: Decimal) -> str:
    """12345.6 → «12 345,60»."""
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def next_number(number: str) -> str:
    """Следующий номер документа: «ПР-15» → «ПР-16», «0099» → «0100»."""
    found = re.search(r"(\d+)(?!.*\d)", number)
    if not found:
        return ""
    digits = found.group(1)
    following = str(int(digits) + 1).zfill(len(digits))
    return number[:found.start()] + following + number[found.end():]


class SaleTab(QWidget):
    """Покупатель, товары, проверка кодов и отправка УПД через «ЭДО Лайт»."""

    # Отправки пишутся в журнал операций соседней вкладки.
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
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._contour = contour
        self._thumbprint = thumbprint
        self._inn = inn
        # Коды как отсканированы, по порядку: строки документа складываются из них.
        self.codes: list[str] = []
        self._keys: set[str] = set()
        # Что вписано по товару — живёт по GTIN, а не по строке таблицы: строки
        # перестраиваются с каждым сканом.
        self._names: dict[str, str] = {}
        self._prices: dict[str, Decimal | None] = {}
        self._origins: dict[str, str] = {}
        self._customs: dict[str, str] = {}
        # Товар без марок: отсканированный, но проданный без кодов (выпущен до
        # начала обязательной маркировки, а марки на нём чужие), и добавленный
        # руками. Количество у них своё — вписанное, а не число кодов.
        self._unmarked: set[str] = set()
        self._manual: list[str] = []
        self._counts: dict[str, int] = {}
        self._manual_seq = 0
        # Ключ товара по строке таблицы: GTIN или ключ ручной строки.
        self._row_keys: list[str] = []
        # Ответ «Честного ЗНАКа» по коду идентификации — с последней проверки.
        self._infos: dict[str, CodeInfo] = {}
        # Документ, судьба отправки которого неизвестна: повтор уходит им же,
        # с тем же именем файла, чтобы оператор узнал его и не создал второй.
        self._pending: sale.Sale | None = None
        self._pending_operation: Operation | None = None
        self._sent: list[Operation] = []
        self._busy = False
        self._filling = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, Metrics.GAP, 0, 0)
        root.setSpacing(Metrics.GAP)
        root.addWidget(self._details_card())
        root.addWidget(self._goods_card(), 1)
        root.addWidget(self._sent_card())
        self._restore()
        self._sync()

    # --- построение ----------------------------------------------------------------

    def _details_card(self) -> Card:
        card = Card(self)
        body = card.body()
        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Покупатель и документ", card))
        header.addStretch(1)
        self.seller_button = self._button(card, "Продавец и подписант…", "card",
                                          self.edit_seller)
        self.seller_button.setToolTip(
            "Реквизиты вашей организации, идентификатор в «ЭДО Лайт» и кто подписывает УПД")
        header.addWidget(self.seller_button)
        body.addLayout(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(9)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self.buyer_inn_edit = QLineEdit(card)
        self.buyer_inn_edit.setPlaceholderText("10 цифр — организация, 12 — ИП")
        self.buyer_inn_edit.setMaxLength(12)
        self.buyer_inn_edit.editingFinished.connect(self._recall_buyer)
        self.buyer_kpp_edit = QLineEdit(card)
        self.buyer_kpp_edit.setPlaceholderText("Для организации")
        self.buyer_kpp_edit.setMaxLength(9)
        self.buyer_name_edit = QLineEdit(card)
        self.buyer_name_edit.setPlaceholderText(
            "Полное название организации или ФИО индивидуального предпринимателя")
        self.buyer_address_edit = QLineEdit(card)
        self.buyer_address_edit.setPlaceholderText("Адрес из ЕГРЮЛ / ЕГРИП")
        self.buyer_edo_edit = QLineEdit(card)
        self.buyer_edo_edit.setPlaceholderText("Например, 2BM-7730306682-…")
        self.buyer_edo_edit.setToolTip(
            "Идентификатор покупателя в его системе ЭДО. Его сообщает покупатель; "
            "в Диадоке он начинается с 2BM-, в «ЭДО Лайт» — с 2LT-.")

        self.number_edit = QLineEdit(card)
        self.number_edit.setPlaceholderText("Номер УПД")
        self.date_edit = QDateEdit(QDate.currentDate(), card)
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd.MM.yyyy")
        self.vat_box = QComboBox(card)
        self.vat_box.addItem("— выберите —", "")
        for rate in sale.VAT_RATES:
            self.vat_box.addItem(rate, rate)
        self.vat_box.currentIndexChanged.connect(self._refresh_sums)
        self.reason_box = QComboBox(card)
        for code, title in sale.WITHDRAWAL_REASONS.items():
            self.reason_box.addItem(title, code)
        self.basis_edit = QLineEdit(card)
        self.basis_edit.setPlaceholderText("Номер договора, если есть")
        self.basis_date_edit = QDateEdit(QDate.currentDate(), card)
        self.basis_date_edit.setCalendarPopup(True)
        self.basis_date_edit.setDisplayFormat("dd.MM.yyyy")
        self.basis_edit.textChanged.connect(self._sync)
        for combo in (self.vat_box, self.reason_box):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)

        rows = (
            ("ИНН покупателя", self.buyer_inn_edit, "КПП", self.buyer_kpp_edit),
            ("Покупатель", self.buyer_name_edit, None, None),
            ("Адрес", self.buyer_address_edit, None, None),
            ("ID ЭДО покупателя", self.buyer_edo_edit, "Вывод из оборота", self.reason_box),
            ("Номер УПД", self.number_edit, "Дата", self.date_edit),
            ("Ставка НДС", self.vat_box, "Договор №", self.basis_edit),
        )
        for row, (first_label, first, second_label, second) in enumerate(rows):
            grid.addWidget(QLabel(first_label, card), row, 0)
            if second is None:
                grid.addWidget(first, row, 1, 1, 3)
                continue
            grid.addWidget(first, row, 1)
            grid.addWidget(QLabel(second_label, card), row, 2)
            grid.addWidget(second, row, 3)
        self.basis_date_label = QLabel("от", card)
        grid.addWidget(self.basis_date_label, len(rows), 2)
        grid.addWidget(self.basis_date_edit, len(rows), 3)
        body.addLayout(grid)

        self.seller_hint = Hint("", card)
        self.seller_hint.setWordWrap(True)
        body.addWidget(self.seller_hint)
        return card

    def _goods_card(self) -> Card:
        card = Card(self)
        body = card.body()
        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Товары", card))
        header.addStretch(1)
        self.paste_button = self._button(card, "Вставить", "copy", self.paste_codes)
        self.file_button = self._button(card, "Из файла", "open", self.load_file)
        self.undo_button = self._button(card, "Отменить скан", "reset", self.undo_scan)
        self.add_button = self._button(card, "Товар без марки", "plus",
                                       self.add_unmarked)
        self.unmark_button = self._button(card, "Без марок", "unlink",
                                          self.toggle_unmarked)
        self.remove_button = self._button(card, "Убрать товар", "trash",
                                          self.remove_selected)
        self.clear_button = self._button(card, "Очистить", "clear", self.clear)
        self.paste_button.setToolTip("Вставить коды из буфера обмена — по одному в строке")
        self.file_button.setToolTip("Взять коды из текстового файла — по одному в строке")
        self.undo_button.setToolTip("Убрать последний отсканированный код")
        self.add_button.setToolTip(
            "Добавить строку товара, который продаётся без кодов: впишите название, "
            "количество и цену")
        self.unmark_button.setToolTip(
            "Продать выбранные товары без кодов — например, выпущенные до начала "
            "обязательной маркировки. Коды не проверяются и в УПД не попадают, "
            "количество можно поправить. Нажмите ещё раз, чтобы вернуть коды")
        self.remove_button.setToolTip("Убрать выбранные товары со всеми их кодами")
        for button in (self.paste_button, self.file_button, self.undo_button,
                       self.add_button, self.unmark_button, self.remove_button,
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
        self.verdict_label.setMinimumHeight(40)
        self.verdict_label.hide()
        body.addWidget(self.verdict_label)

        self.table = QTableWidget(0, len(COLUMNS), card)
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed
                                   | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.table.setMinimumHeight(150)
        self.table.horizontalHeaderItem(PRICE).setToolTip(
            "Цена за штуку с НДС, ₽ — двойной щелчок, чтобы вписать")
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Stretch)
        for column in range(len(COLUMNS)):
            if column != NAME:
                head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        head.setMinimumSectionSize(64)
        self.table.setItemDelegate(_CellEditor(self.table))
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._sync)
        body.addWidget(self.table, 1)

        self.total_label = QLabel("", card)
        self.total_label.setObjectName("SectionTitle")
        body.addWidget(self.total_label)

        self.hint = Hint("", card)
        self.hint.setWordWrap(True)
        body.addWidget(self.hint)
        actions = QHBoxLayout()
        actions.setSpacing(9)
        actions.addStretch(1)
        self.check_button = self._button(card, "Проверить коды", "marking",
                                         self.check_codes)
        self.check_button.setToolTip(
            "Спросить «Честный ЗНАК»: наши ли это коды и в обороте ли они. "
            "Пустые наименования подставятся из ответа")
        self.save_button = self._button(card, "Сохранить для Диадока…", "save",
                                        self.save_for_diadoc)
        self.save_button.setToolTip(
            "Проверить коды и сохранить УПД с вашим ID в Диадоке. Файл загружается "
            "в Диадок, подписывается и отправляется там — покупатель получит его "
            "по роумингу Диадока")
        self.send_button = self._button(card, "Отправить через ЭДО Лайт", "export",
                                        self.send_document)
        self.send_button.setToolTip(
            "Подписать и отправить из программы через «ЭДО Лайт». Нужен роуминг "
            "«ЭДО Лайт» с оператором покупателя")
        # Основной путь — Диадок: роуминг с покупателями настроен там.
        self.save_button.setObjectName("Primary")
        for button in (self.check_button, self.send_button, self.save_button):
            actions.addWidget(button)
        body.addLayout(actions)
        return card

    def _sent_card(self) -> Card:
        card = Card(self)
        body = card.body()
        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Отправленные УПД", card))
        header.addStretch(1)
        self.refresh_button = self._button(card, "Обновить статусы", "refresh",
                                           self.refresh_statuses)
        self.refresh_button.setToolTip(
            "Спросить у «ЭДО Лайт», подписал ли покупатель, а у «Честного ЗНАКа» — "
            "выбыли ли коды")
        header.addWidget(self.refresh_button)
        body.addLayout(header)
        self.sent_table = QTableWidget(0, 5, card)
        self.sent_table.setHorizontalHeaderLabels(
            ["Отправлен", "Документ", "Кодов", "Контур", "Статус"])
        self.sent_table.verticalHeader().setVisible(False)
        self.sent_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sent_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sent_table.setMinimumHeight(90)
        self.sent_table.setMaximumHeight(140)
        head = self.sent_table.horizontalHeader()
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for column in (0, 2, 3):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        body.addWidget(self.sent_table)
        self.sent_hint = Hint(
            "Коды выбывают из оборота, когда покупатель подпишет УПД. Пока он не "
            "подписал, они числятся за вами.", card)
        self.sent_hint.setWordWrap(True)
        body.addWidget(self.sent_hint)
        return card

    @staticmethod
    def _button(parent: QWidget, text: str, icon: str,
                handler: Callable[[], None]) -> QPushButton:
        button = QPushButton(text, parent)
        button.setIcon(icons.icon(icon))
        button.clicked.connect(lambda _checked=False: handler())
        return button

    # --- запомненное ---------------------------------------------------------------

    def _restore(self) -> None:
        seller = self.settings.marking_sale_seller
        index = self.vat_box.findData(seller.get("vat", ""))
        self.vat_box.setCurrentIndex(max(0, index))
        index = self.reason_box.findData(seller.get("reason", sale.DEFAULT_REASON))
        self.reason_box.setCurrentIndex(max(0, index))
        self.number_edit.setText(next_number(seller.get("last_number", "")))
        completer = QCompleter(sorted(self.settings.marking_sale_buyers), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.buyer_inn_edit.setCompleter(completer)
        self._show_seller()

    def _recall_buyer(self) -> None:
        """Знакомый покупатель: по ИНН подставляется то, что вписывали раньше."""
        known = self.settings.marking_sale_buyers.get(self.buyer_inn_edit.text().strip())
        if not known:
            return
        for edit, name in ((self.buyer_kpp_edit, "kpp"), (self.buyer_name_edit, "name"),
                           (self.buyer_address_edit, "address"),
                           (self.buyer_edo_edit, "edo_id")):
            if not edit.text().strip():
                edit.setText(known.get(name, ""))

    def _remember(self, document: sale.Sale) -> None:
        """После отправки: покупатель, товары, номер, ставка и причина — на будущее."""
        buyer = document.buyer
        self.settings.marking_sale_buyers[buyer.inn.strip()] = {
            "name": buyer.name.strip(), "kpp": buyer.kpp.strip(),
            "address": buyer.address.strip(), "edo_id": buyer.edo_id.strip()}
        for line in document.lines:
            if not line.gtin:
                continue
            self.settings.marking_sale_items[line.gtin] = {
                "name": line.name.strip(), "price": str(line.price or ""),
                "origin": line.origin.strip(), "customs": line.customs.strip()}
        seller = dict(self.settings.marking_sale_seller)
        seller.update(last_number=document.number.strip(), vat=document.vat,
                      reason=document.reason)
        self.settings.marking_sale_seller = seller
        self.settings.save()

    # --- продавец ------------------------------------------------------------------

    def edit_seller(self) -> None:
        dialog = SellerDialog(self.settings.marking_sale_seller, self._thumbprint(),
                              self._inn(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        seller = dict(self.settings.marking_sale_seller)
        seller.update(dialog.values())
        self.settings.marking_sale_seller = seller
        self.settings.save()
        self._show_seller()
        self._sync()

    def seller(self, diadoc: bool = False) -> sale.Party:
        """Продавец. Идентификатор ЭДО — того оператора, из которого уйдёт УПД:
        в Диадоке и в «ЭДО Лайт» у одной организации они разные."""
        values = self.settings.marking_sale_seller
        return sale.Party(inn=values.get("inn", "") or self._inn(),
                          name=values.get("name", ""), kpp=values.get("kpp", ""),
                          address=values.get("address", ""),
                          edo_id=values.get("diadoc_id" if diadoc else "edo_id", ""))

    def signer(self) -> sale.Signer:
        values = self.settings.marking_sale_seller
        return sale.Signer(surname=values.get("surname", ""),
                           name=values.get("first_name", ""),
                           patronymic=values.get("patronymic", ""),
                           position=values.get("position", ""),
                           poa_number=values.get("poa_number", ""),
                           poa_date=values.get("poa_date", ""),
                           poa_system=values.get("poa_system", "") or sale.POA_SYSTEM)

    def _show_seller(self) -> None:
        values = self.settings.marking_sale_seller
        problems = seller_problems(values | {"inn": self.seller().inn})
        if problems:
            self.seller_hint.setStyleSheet(f"color: {Palette.WARNING};")
            self.seller_hint.setText(
                "Заполните «Продавец и подписант»: " + problems[0] + ".")
            return
        seller, signer = self.seller(), self.signer()
        ids = " · ".join(f"{name} {values[key]}" for key, name in
                         (("diadoc_id", "Диадок"), ("edo_id", "ЭДО Лайт"))
                         if values.get(key, "").strip())
        self.seller_hint.setStyleSheet("")
        poa = " по МЧД" if signer.by_poa else ""
        self.seller_hint.setText(
            f"Продавец: {seller.title}, ИНН {seller.inn} · {ids} · "
            f"подписывает {signer.title}{poa}.")

    # --- скан ----------------------------------------------------------------------

    def focus_scan(self) -> None:
        self.scan_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def accept_scan(self) -> None:
        text = self.scan_edit.text().strip()
        self.scan_edit.clear()
        if text:
            self.add_code(text)

    def add_code(self, text: str, *, quiet: bool = False) -> str:
        """Добавляет код. Возвращает `added`, `repeat` или `broken`."""
        text = codes_module.from_keyboard((text or "").strip())
        parsed = codes_module.parse(text)
        if not text or not parsed.valid or not parsed.ki:
            if not quiet:
                reason = parsed.problems[0] if parsed.problems else "код не разобран"
                self._say("broken", f"Не принято: {reason}")
            return "broken"
        key = key_of(text)
        if key in self._keys:
            if not quiet:
                self._say("repeat", f"Уже в документе · {parsed.gtin} · {parsed.serial}")
            return "repeat"
        self._keys.add(key)
        self.codes.append(text)
        self._learn(parsed.gtin)
        if not quiet:
            name = self._names.get(parsed.gtin) or parsed.gtin
            self._say("added", f"Принято · {name} · {parsed.serial}")
            self._rebuild()
        return "added"

    def _learn(self, gtin: str) -> None:
        """Новый товар: подставить запомненное по нему с прошлых продаж."""
        if gtin in self._prices:
            return
        known = self.settings.marking_sale_items.get(gtin, {})
        self._names.setdefault(gtin, known.get("name", ""))
        self._prices[gtin] = sale.money(known.get("price", ""))
        self._origins.setdefault(gtin, known.get("origin", ""))
        self._customs.setdefault(gtin, known.get("customs", ""))

    def add_many(self, text: str) -> tuple[int, int, int]:
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
                      f"Добавлено кодов: {added}" + (" · " + ", ".join(tail) if tail else ""))
        self._rebuild()
        return added, repeats, broken

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

    def undo_scan(self) -> None:
        if not self.codes:
            return
        code = self.codes.pop()
        self._keys.discard(key_of(code))
        parsed = codes_module.parse(code)
        self._say("repeat", f"Скан отменён · {parsed.gtin} · {parsed.serial}")
        self._rebuild()

    def _selected_keys(self) -> list[str]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [self._row_keys[row] for row in rows if row < len(self._row_keys)]

    def remove_selected(self) -> None:
        keys = set(self._selected_keys())
        if not keys:
            return
        kept = [code for code in self.codes if codes_module.parse(code).gtin not in keys]
        self.codes = kept
        self._keys = {key_of(code) for code in kept}
        self._manual = [key for key in self._manual if key not in keys]
        self._unmarked -= keys
        for key in keys:
            self._counts.pop(key, None)
        self._rebuild()

    def add_unmarked(self) -> None:
        """Строка товара без марок: название, количество и цену человек впишет сам."""
        self._manual_seq += 1
        key = f"{MANUAL_KEY}{self._manual_seq}"
        self._manual.append(key)
        self._counts[key] = 1
        self._rebuild()
        row = self._row_keys.index(key)
        self.table.setCurrentCell(row, NAME)
        self.table.editItem(self.table.item(row, NAME))

    def toggle_unmarked(self) -> None:
        """Выбранные отсканированные товары — без кодов или обратно с кодами.

        Коды остаются в списке: вернуть их можно тем же нажатием. В УПД товар без
        марок идёт без кодов и без GTIN, и «Честный ЗНАК» о нём не спрашивается.
        """
        keys = [key for key in self._selected_keys() if key not in self._manual]
        if not keys:
            return
        if all(key in self._unmarked for key in keys):
            self._unmarked -= set(keys)
            for key in keys:
                self._counts.pop(key, None)
        else:
            self._unmarked |= set(keys)
        self._rebuild()

    def clear(self) -> None:
        self.codes.clear()
        self._keys.clear()
        for key in self._manual:
            for values in (self._names, self._prices, self._origins, self._customs):
                values.pop(key, None)
        self._manual.clear()
        self._unmarked.clear()
        self._counts.clear()
        self._infos.clear()
        self._pending = None
        self._pending_operation = None
        self.verdict_label.hide()
        self.hint.setText("")
        self.hint.setStyleSheet("")
        self._rebuild()
        self.focus_scan()

    def _say(self, kind: str, text: str) -> None:
        colour, background = RESULT_COLOURS[kind]
        self.verdict_label.setText(text)
        self.verdict_label.show()
        self.verdict_label.setStyleSheet(
            f"color: {colour}; background: {background}; font-size: 17px;"
            f" font-weight: 700; border-radius: {Metrics.RADIUS}px; padding: 7px 14px;")
        if kind != "added" and self.settings.marking_reconcile_sound:
            QApplication.beep()

    # --- таблица товаров -----------------------------------------------------------

    def _rows(self) -> list[tuple[str, sale.Line]]:
        """Товары документа с ключами строк: сначала отсканированные, потом ручные."""
        rows: list[tuple[str, sale.Line]] = []
        for line in sale.lines_from_codes(self.codes, self._names, self._prices,
                                          self._origins, self._customs):
            if line.gtin in self._unmarked:
                line = replace(line, codes=(),
                               count=self._counts.get(line.gtin, len(line.codes)))
            rows.append((line.gtin, line))
        for key in self._manual:
            rows.append((key, sale.Line(
                gtin="", name=self._names.get(key, ""), price=self._prices.get(key),
                codes=(), origin=self._origins.get(key, ""),
                customs=self._customs.get(key, ""), count=self._counts.get(key, 1))))
        return rows

    def lines(self) -> list[sale.Line]:
        return [line for _, line in self._rows()]

    def _marked_identifiers(self) -> list[str]:
        """Коды, которые уйдут в УПД, — без кодов товаров, проданных без марок."""
        return [item for line in self.lines() for item in line.identifiers]

    def _rebuild(self) -> None:
        """Перестраивает таблицу по кодам: строка на товар."""
        self._filling = True
        try:
            rows = self._rows()
            self._row_keys = [key for key, _ in rows]
            self.table.setRowCount(len(rows))
            vat = self.vat_box.currentData() or sale.NO_VAT
            for row, (_, line) in enumerate(rows):
                price = "" if line.price is None else rubles(line.price)
                cells = (line.gtin, line.name, str(line.quantity), price,
                         rubles(line.amounts(vat).total) if line.price else "",
                         line.origin, line.customs, self._check_text(line))
                for column, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    editable = column in EDITABLE and (column != QUANTITY
                                                       or not line.marked)
                    if not editable:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if column in (QUANTITY, PRICE, SUM):
                        item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                              | Qt.AlignmentFlag.AlignVCenter)
                    if column == CHECK:
                        item.setForeground(self._check_colour(line))
                    self.table.setItem(row, column, item)
        finally:
            self._filling = False
        self._refresh_total()
        self._sync()

    def _check_text(self, line: sale.Line) -> str:
        if not line.marked:
            return UNMARKED_TEXT
        infos = [self._infos.get(identifier) for identifier in line.identifiers]
        known = [info for info in infos if info is not None]
        if not known:
            return "не проверены"
        bad = len(sale.code_problems(known, self.seller().inn))
        if bad:
            return f"нельзя продать: {bad} из {line.quantity}"
        if len(known) < line.quantity:
            return f"проверено {len(known)} из {line.quantity}"
        return f"можно продавать ({line.quantity})"

    def _check_colour(self, line: sale.Line) -> QColor:
        text = self._check_text(line)
        if text.startswith("нельзя"):
            return QColor(Palette.DANGER)
        if text.startswith("можно"):
            return QColor(Palette.SUCCESS)
        return QColor(Palette.TEXT_MUTED)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling or item.column() not in EDITABLE \
                or item.row() >= len(self._row_keys):
            return
        gtin, text = self._row_keys[item.row()], item.text().strip()
        if item.column() == NAME:
            self._names[gtin] = text
        elif item.column() == QUANTITY:
            if text.isdigit() and int(text) > 0:
                self._counts[gtin] = int(text)
            else:
                self._tell(f"Количество «{text}» — нужно целое число больше нуля",
                           ToastKind.WARNING)
        elif item.column() == PRICE:
            price = sale.money(text)
            if text and price is None:
                self._tell(f"Цена «{text}» не похожа на число", ToastKind.WARNING)
            self._prices[gtin] = price
        elif item.column() == ORIGIN:
            self._origins[gtin] = text
        elif item.column() == CUSTOMS:
            self._customs[gtin] = text
        self._rebuild()

    def _refresh_sums(self) -> None:
        self._rebuild()

    def _refresh_total(self) -> None:
        lines = self.lines()
        if not lines:
            self.total_label.setText("")
            return
        vat = self.vat_box.currentData() or ""
        count = sum(line.quantity for line in lines)
        bare = sum(line.quantity for line in lines if not line.marked)
        tail = f" (из них {UNMARKED_TEXT} {bare})" if bare and bare != count \
            else f" {UNMARKED_TEXT}" if bare else ""
        if any(line.price is None for line in lines) or not vat:
            self.total_label.setText(
                f"Итого: {count} шт.{tail} · укажите цены и ставку НДС")
            return
        totals = sum((line.amounts(vat) for line in lines), sale.Amounts())
        tax = "без НДС" if vat == sale.NO_VAT else f"в т. ч. НДС {vat} — {rubles(totals.vat)} ₽"
        self.total_label.setText(
            f"Итого: {count} шт.{tail} на {rubles(totals.total)} ₽, {tax}")

    # --- документ ------------------------------------------------------------------

    def document(self, diadoc: bool = False) -> sale.Sale:
        """Документ из того, что на экране. Проверяется отдельно: `problems`."""
        basis = self.basis_edit.text().strip()
        return sale.Sale(
            number=self.number_edit.text().strip(),
            date=self.date_edit.date().toString("yyyy-MM-dd"),
            seller=self.seller(diadoc),
            buyer=sale.Party(inn=self.buyer_inn_edit.text().strip(),
                             name=self.buyer_name_edit.text().strip(),
                             kpp=self.buyer_kpp_edit.text().strip(),
                             address=self.buyer_address_edit.text().strip(),
                             edo_id=self.buyer_edo_edit.text().strip()),
            signer=self.signer(),
            lines=tuple(self.lines()),
            vat=self.vat_box.currentData() or "",
            reason=self.reason_box.currentData() or sale.DEFAULT_REASON,
            basis_name="Договор" if basis else "",
            basis_number=basis,
            basis_date=self.basis_date_edit.date().toString("yyyy-MM-dd") if basis else "")

    def _problem_text(self, problems: list[str]) -> str:
        text = problems[0]
        if "нет наименования" in text:
            text += (" — впишите его или, если товар с марками, нажмите «Проверить "
                     "коды»: названия подставятся из «Честного ЗНАКа»")
        return text

    def save_for_diadoc(self) -> None:
        """УПД для Диадока: проверка кодов → файл → запись в журнале.

        Отправляет его человек — загружает файл в Диадок, подписывает и
        отправляет. Покупатель в СБИС или другом ЭДО получит его по роумингу
        Диадока. Коды проверяются заранее так же, как при отправке из программы:
        чужой или выбывший код выяснился бы только после подписи покупателя.
        """
        document = self.document(diadoc=True)
        if not self._ready(document, signing=False):
            return
        self._start_check(document, then=self._write_for_diadoc)

    def _write_for_diadoc(self, document: sale.Sale) -> None:
        # Формат требует, чтобы имя файла совпадало с ИдФайл: под другим именем
        # оператор файл не примет.
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить УПД для Диадока", document.file_name, "XML (*.xml)")
        if not path:
            self.hint.setText("Файл не сохранён.")
            return
        if os.path.splitext(os.path.basename(path))[0] != document.file_id:
            self._tell("Имя файла должно совпадать с идентификатором внутри — иначе "
                       "Диадок его не примет. Сохраните под предложенным именем.",
                       ToastKind.WARNING)
            return
        try:
            with open(path, "wb") as handle:
                handle.write(document.render())
        except (OSError, sale.SaleProblem) as error:
            self._tell(f"Не удалось сохранить файл: {error}", ToastKind.ERROR)
            return
        earlier: list[Operation] = []
        # Журнал — о кодах: УПД целиком без марок в него не пишется, следить
        # по нему не за чем.
        if document.marked:
            earlier = service.warn_duplicates(
                Operation(kind=OperationKind.SHIP, codes=document.identifiers))
            operation = service.start(
                OperationKind.SHIP, document.identifiers, contour=self._contour(),
                reason=document.reason, comment=_describe(document) + " · Диадок")
            operation.status = OperationStatus.SENT
            operation.sent_at = datetime.now()
            operation.document_id = edo.MANUAL_PREFIX + document.file_id
            operation.error = edo.EdoStatus(manual=True).title
            service.store.update(operation)
            self.journal_changed.emit()
        self._remember(document)
        self.clear()
        self.number_edit.setText(next_number(document.number))
        self.reload_sent()
        text = (f"УПД № {document.number} сохранён: {path}. Загрузите его в Диадок "
                "(«Отправить документ» → «Загрузить из файла»), подпишите и "
                "отправьте покупателю.")
        if document.marked:
            text += " Коды выбудут, когда он подпишет."
        if earlier:
            text += " Внимание: эти коды уже продавали раньше — проверьте журнал."
        self.hint.setStyleSheet(f"color: {Palette.WARNING};" if earlier else "")
        self.hint.setText(text)
        self._tell(text, ToastKind.WARNING if earlier else ToastKind.SUCCESS)

    def _ready(self, document: sale.Sale, signing: bool = True) -> bool:
        """Можно ли отправлять: документ собран, вход выполнен там же, продавец тот же.

        Вход нужен, чтобы подписать документ или спросить «Честный ЗНАК» о кодах.
        Файл для Диадока на товар без марок собирается и без него.
        """
        if problems := document.problems:
            if signing:
                self._tell(f"УПД не отправлен: {self._problem_text(problems)}",
                           ToastKind.WARNING)
            else:
                self._tell(f"Файл не собран: {self._problem_text(problems)}",
                           ToastKind.WARNING)
            return False
        if signing and not self._thumbprint():
            self._tell("УПД подписывается — выберите сертификат на вкладке "
                       "«Проверка кодов»", ToastKind.WARNING)
            return False
        if not signing and not document.marked:
            return True
        if not service.signed_in():
            self._tell("Коды проверяются в «Честном ЗНАКе» от имени организации — "
                       "войдите по сертификату на вкладке «Проверка кодов»",
                       ToastKind.WARNING)
            return False
        if service.current().contour != self._contour():
            self._tell("Вход выполнен в другом контуре — войдите заново", ToastKind.WARNING)
            return False
        signed_inn = (service.current().inn or "").strip()
        if signed_inn and signed_inn != document.seller.inn.strip():
            self._tell(f"Продавец в УПД (ИНН {document.seller.inn}) не тот, под кем "
                       f"выполнен вход (ИНН {signed_inn})", ToastKind.WARNING)
            return False
        return True

    def check_codes(self) -> None:
        """Спрашивает «Честный ЗНАК» о кодах. Ничего не отправляет."""
        if not self._marked_identifiers():
            return
        if not service.signed_in():
            self._tell("Чтобы спросить «Честный ЗНАК», войдите по сертификату на "
                       "вкладке «Проверка кодов»", ToastKind.WARNING)
            return
        self._start_check(None)

    def send_document(self) -> None:
        """Проверка кодов → подтверждение → подпись и отправка покупателю."""
        document = self._document_for_retry(self.document())
        if not self._ready(document):
            return
        self._start_check(document)

    def _document_for_retry(self, document: sale.Sale) -> sale.Sale:
        """Тот же документ после неизвестного исхода — то же имя файла.

        Сравнивается весь документ, а не только коды: под тем же именем оператор
        вернул бы уже загруженный файл, и поправленная цена или товар без марок
        до покупателя не дошли бы.
        """
        pending = self._pending
        if pending is None:
            return document
        same = replace(document, guid=pending.guid, created=pending.created)
        return same if same == pending else document

    def _start_check(self, document: sale.Sale | None,
                     then: Callable[[sale.Sale], None] | None = None) -> None:
        """Проверка кодов, а после неё — `then(document)` или отправка через «ЭДО Лайт».

        Спрашиваются только коды, которые уйдут в УПД: о товаре без марок
        «Честному ЗНАКу» сказать нечего. Если кодов нет вовсе, проверки нет.
        """
        identifiers = document.identifiers if document is not None \
            else self._marked_identifiers()
        if not identifiers:
            if document is not None:
                self._after_check(document, [], [], then)
            return
        self._busy = True
        self._sync()
        self.hint.setStyleSheet("")
        self.hint.setText("Спрашиваем «Честный ЗНАК», чьи это коды и в обороте ли они…")
        run_task(_check, identifiers, self._contour(),
                 on_result=lambda infos: self._after_check(document, identifiers,
                                                           infos, then),
                 on_error=self._on_check_error)

    def _on_check_error(self, message: str) -> None:
        self._busy = False
        self._sync()
        self.hint.setStyleSheet(f"color: {Palette.DANGER};")
        self.hint.setText(f"«Честный ЗНАК» не ответил: {message}")
        self._tell(f"Коды не проверены: {message}", ToastKind.ERROR)

    def _after_check(self, document: sale.Sale | None, identifiers: list[str],
                     infos: list[CodeInfo],
                     then: Callable[[sale.Sale], None] | None = None) -> None:
        self._busy = False
        for info in infos:
            self._infos[codes_module.for_request(info.code) or info.code] = info
            gtin = info.gtin or codes_module.parse(info.code).gtin
            if gtin and not self._names.get(gtin) and info.product_name:
                self._names[gtin] = info.product_name
        self._rebuild()
        asked = set(identifiers)
        answered = [self._infos[key] for key in asked if key in self._infos]
        problems = sale.code_problems(answered, self.seller().inn)
        missing = len(asked) - len(answered)
        if missing:
            problems.append(f"«Честный ЗНАК» не ответил по {missing} кодам")
        if problems:
            shown = "; ".join(problems[:3]) + (f" и ещё {len(problems) - 3}"
                                               if len(problems) > 3 else "")
            self.hint.setStyleSheet(f"color: {Palette.DANGER};")
            self.hint.setText(
                f"Продать нельзя: {shown}. Если товар выпущен до начала обязательной "
                "маркировки, выделите его строку и нажмите «Без марок» — он уйдёт "
                "в УПД без кодов.")
            self._tell(f"Продать нельзя: {problems[0]}", ToastKind.ERROR)
            return
        if document is None:
            self.hint.setStyleSheet("")
            self.hint.setText(f"Все {len(asked)} кодов наши и в обороте — можно продавать.")
            self._tell("Коды проверены: можно продавать", ToastKind.SUCCESS)
            return
        if then is not None:
            then(document)
            return
        self._confirm_and_send(document)

    def _confirm_and_send(self, document: sale.Sale) -> None:
        contour = self._contour()
        retry = self._pending is not None and document.guid == self._pending.guid
        earlier = [] if retry or not document.marked else service.warn_duplicates(
            Operation(kind=OperationKind.SHIP, codes=document.identifiers))
        if not self._confirm(document, contour, earlier, retry):
            self.hint.setText("Отправка отменена.")
            return
        operation = self._pending_operation if retry else None
        # Журнал — о кодах: УПД целиком без марок отправляется без записи в нём.
        if operation is None and document.marked:
            operation = service.start(
                OperationKind.SHIP, document.identifiers, contour=contour,
                reason=document.reason, comment=_describe(document))
        self._journal(operation, status=OperationStatus.SENDING, sent_at=datetime.now(),
                      error="")

        self._busy = True
        self._sync()
        self.hint.setStyleSheet("")
        self.hint.setText("Подписываем УПД и отправляем покупателю — КриптоПро может "
                          "спросить пароль…")
        run_task(_send, document, self._thumbprint(), contour,
                 on_result=lambda outcome: self._on_sent(operation, document, outcome),
                 on_error=lambda message: self._on_send_error(operation, message))

    def _confirm(self, document: sale.Sale, contour: Contour,
                 earlier: list[Operation], retry: bool) -> bool:
        totals = document.totals
        tax = "без НДС" if document.vat == sale.NO_VAT \
            else f"в т. ч. НДС {document.vat} {rubles(totals.vat)} ₽"
        bare = sum(line.quantity for line in document.lines if not line.marked)
        lines = [f"Покупатель: {document.buyer.title}, ИНН {document.buyer.inn}.",
                 f"УПД № {document.number} от {self.date_edit.date().toString('dd.MM.yyyy')}: "
                 f"{document.quantity} шт. на {rubles(totals.total)} ₽, {tax}."]
        if document.marked:
            lines.append(f"Вывод из оборота ({len(document.identifiers)} кодов): "
                         f"{sale.WITHDRAWAL_REASONS[document.reason].lower()}.")
        if bare:
            lines.append(f"Без марок: {bare} шт. — уходят без кодов.")
        lines.append(f"Контур: {contour.title}. Через «ЭДО Лайт» на {document.buyer.edo_id}.")
        if retry:
            lines.append("\nЭто повтор того же документа: если он уже дошёл, второго "
                         "не будет.")
        elif earlier:
            when = earlier[0].created_at
            stamp = f" {when:%d.%m.%Y %H:%M}" if when else ""
            lines.append(f"\nВНИМАНИЕ: эти коды уже продавали{stamp} "
                         f"({(earlier[0].error or earlier[0].status.title).lower()}).")
        if document.marked:
            lines.append("\nКогда покупатель подпишет УПД, коды выбудут из оборота. "
                         "Отменить это можно только аннулированием документа.")
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Продажа с выводом из оборота" if document.marked
                               else "Продажа без марок")
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setText(f"Отправить покупателю УПД на {document.quantity} шт.?")
        confirm.setInformativeText("\n".join(lines))
        yes = confirm.addButton("Подписать и отправить",
                                QMessageBox.ButtonRole.DestructiveRole)
        cancel = confirm.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        confirm.setDefaultButton(cancel)
        confirm.exec()
        return confirm.clickedButton() is yes

    def _journal(self, operation: Operation | None, **changes) -> None:
        """Отметка об отправке в журнале — если документ в нём есть (есть коды)."""
        if operation is None:
            return
        for name, value in changes.items():
            setattr(operation, name, value)
        service.store.update(operation)
        self.journal_changed.emit()

    def _on_sent(self, operation: Operation | None, document: sale.Sale,
                 outcome: tuple) -> None:
        kind, payload = outcome
        self._busy = False
        if kind == "unknown":
            # Документ мог дойти. Коды остаются на месте, а повтор уйдёт тем же
            # файлом — оператор узнает его и второго документа не создаст.
            self._pending, self._pending_operation = document, operation
            self._journal(operation, status=OperationStatus.SENT, error="исход неизвестен")
            self._sync()
            self.hint.setStyleSheet(f"color: {Palette.WARNING};")
            self.hint.setText(payload)
            self._tell(payload, ToastKind.WARNING)
            return
        self._journal(operation, document_id=payload, status=OperationStatus.SENT,
                      error=edo.STATUS_TITLES[1])
        self._remember(document)
        self.clear()
        self.number_edit.setText(next_number(document.number))
        self.reload_sent()
        text = f"УПД № {document.number} отправлен покупателю."
        if document.marked:
            text += " Коды выбудут из оборота, когда он подпишет документ."
        self.hint.setStyleSheet("")
        self.hint.setText(text)
        self._tell(text, ToastKind.SUCCESS)

    def _on_send_error(self, operation: Operation | None, message: str) -> None:
        """Не отправлено: подпись, вход или отказ оператора — до создания документа."""
        self._busy = False
        self._journal(operation, status=OperationStatus.FAILED, error=message)
        if self._pending is not None and self._pending_operation is operation:
            self._pending = self._pending_operation = None
        self.reload_sent()
        self._sync()
        self.hint.setStyleSheet(f"color: {Palette.DANGER};")
        self.hint.setText(f"УПД не отправлен: {message}")
        self._tell(f"УПД не отправлен: {message}", ToastKind.ERROR)

    # --- отправленные --------------------------------------------------------------

    def reload_sent(self) -> None:
        """Отправленные УПД — из журнала операций на этом компьютере."""
        operations = [operation for operation in service.history(200)
                      if operation.kind is OperationKind.SHIP][:SENT_LIMIT]
        self._sent = operations
        self.sent_table.setRowCount(len(operations))
        for row, operation in enumerate(operations):
            moment = operation.sent_at or operation.created_at
            cells = (f"{moment:%d.%m.%Y %H:%M}" if moment else "—", operation.comment,
                     str(operation.size), operation.contour.title,
                     operation.error or operation.status.title)
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 4:
                    colour = {OperationStatus.DONE: Palette.SUCCESS,
                              OperationStatus.REJECTED: Palette.DANGER,
                              OperationStatus.FAILED: Palette.DANGER}.get(operation.status)
                    if colour:
                        item.setForeground(QColor(colour))
                self.sent_table.setItem(row, column, item)
        self._sync()

    def _waiting(self) -> list[Operation]:
        return [operation for operation in self._sent
                if operation.status is OperationStatus.SENT and operation.document_id]

    def refresh_statuses(self) -> None:
        waiting = self._waiting()
        if not waiting:
            self._tell("Ждущих ответа УПД нет", ToastKind.INFO)
            return
        if not service.signed_in():
            self._tell("Статус спрашивается от имени организации — войдите по "
                       "сертификату на вкладке «Проверка кодов»", ToastKind.WARNING)
            return
        contour = service.current().contour
        here = [operation for operation in waiting if operation.contour == contour]
        if not here:
            self._tell("Ждущие УПД отправлены в другом контуре — войдите в нём",
                       ToastKind.WARNING)
            return
        self._busy = True
        self._sync()
        self.sent_hint.setStyleSheet("")
        self.sent_hint.setText("Спрашиваем «ЭДО Лайт» и «Честный ЗНАК»…")
        run_task(_follow, here, on_result=self._on_followed,
                 on_error=self._on_follow_error)

    def _on_followed(self, results: list) -> None:
        self._busy = False
        failures: list[str] = []
        for operation, outcome, error in results:
            if outcome is None:
                failures.append(error)
                continue
            operation.status = outcome.operation_status
            operation.error = outcome.text
            if operation.status is OperationStatus.DONE:
                operation.checked_at = datetime.now()
            service.store.update(operation)
        self.journal_changed.emit()
        self.reload_sent()
        if failures:
            self.sent_hint.setStyleSheet(f"color: {Palette.WARNING};")
            self.sent_hint.setText(f"Не по всем УПД есть ответ: {failures[0]}")
        else:
            self.sent_hint.setStyleSheet("")
            self.sent_hint.setText(f"Статусы обновлены: {datetime.now():%H:%M}.")

    def _on_follow_error(self, message: str) -> None:
        self._busy = False
        self._sync()
        self.sent_hint.setStyleSheet(f"color: {Palette.DANGER};")
        self.sent_hint.setText(f"Статусы не получены: {message}")

    # --- состояние -----------------------------------------------------------------

    def _tell(self, text: str, kind: ToastKind) -> None:
        if self.notify:
            self.notify(text, kind)

    def _sync(self) -> None:
        has_codes = bool(self.codes)
        has_lines = bool(self._row_keys)
        free = not self._busy
        selected = bool(self.table.selectionModel()
                        and self.table.selectionModel().hasSelection())
        scanned = [key for key in self._selected_keys() if key not in self._manual] \
            if selected else []
        for widget in (self.scan_edit, self.paste_button, self.file_button,
                       self.add_button):
            widget.setEnabled(free)
        self.undo_button.setEnabled(has_codes and free)
        self.remove_button.setEnabled(selected and free)
        self.unmark_button.setEnabled(bool(scanned) and free)
        self.unmark_button.setText(
            "С марками" if scanned and all(key in self._unmarked for key in scanned)
            else "Без марок")
        self.clear_button.setEnabled(has_lines and free)
        self.check_button.setEnabled(bool(self._marked_identifiers()) and free)
        self.save_button.setEnabled(has_lines and free)
        self.send_button.setEnabled(has_lines and free)
        self.send_button.setText("Отправить повторно" if self._pending is not None
                                 else "Отправить через ЭДО Лайт")
        self.refresh_button.setEnabled(free and bool(self._waiting()))
        has_basis = bool(self.basis_edit.text().strip())
        self.basis_date_label.setEnabled(has_basis)
        self.basis_date_edit.setEnabled(has_basis)
        if free and not self.hint.text():
            self.hint.setText(
                "Отсканируйте товар, впишите цены и нажмите «Сохранить для Диадока»: "
                "коды проверятся в «Честном ЗНАКе», а файл останется загрузить в Диадок.")


def _describe(document: sale.Sale) -> str:
    """Подпись УПД в журнале: номер, дата, покупатель и сумма."""
    year, month, day = document.date.split("-")
    return (f"УПД № {document.number} от {day}.{month}.{year} · "
            f"{document.buyer.title} (ИНН {document.buyer.inn}) · "
            f"{rubles(document.totals.total)} ₽")


def _check(identifiers: list[str], contour: Contour) -> list[CodeInfo]:
    """Свежий ответ «Честного ЗНАКа»: вчерашнее «в обороте» для продажи не годится."""
    return service.check(identifiers, contour=contour, use_cache=False)


def _send(document: sale.Sale, thumbprint: str, contour: Contour) -> tuple[str, str]:
    """Отправка в фоне. Неизвестный исход — значением, а не ошибкой: он лечится иначе."""
    try:
        return "sent", edo.send(document, thumbprint, contour)
    except edo.EdoUnknown as error:
        return "unknown", str(error)


def _follow(operations: list[Operation]) -> list[tuple[Operation, edo.Outcome | None, str]]:
    """Статусы по очереди. Сбой одного УПД не мешает узнать про остальные."""
    results: list[tuple[Operation, edo.Outcome | None, str]] = []
    for operation in operations:
        try:
            outcome = edo.follow(operation.document_id, operation.codes,
                                 operation.contour, since=operation.sent_at)
        except (transport.MarkingError, crypto.SigningFailed) as error:
            results.append((operation, None, str(error)))
            continue
        results.append((operation, outcome, ""))
    return results


def seller_problems(values: dict[str, str]) -> list[str]:
    """Чего не хватает в реквизитах продавца и подписанта.

    Идентификаторов ЭДО два, и нужен хотя бы один: в Диадоке — для файла, в
    «ЭДО Лайт» — для отправки из программы. Вписанный проверяется на вид.
    """
    signer = sale.Signer(values.get("surname", ""), values.get("first_name", ""),
                         values.get("patronymic", ""), values.get("position", ""),
                         values.get("poa_number", ""), values.get("poa_date", ""),
                         values.get("poa_system", "") or sale.POA_SYSTEM)
    ids = [values.get(key, "").strip() for key in ("diadoc_id", "edo_id")]
    found: list[str] = []
    for edo_id in [item for item in ids if item] or [""]:
        party = sale.Party(inn=values.get("inn", ""), name=values.get("name", ""),
                           kpp=values.get("kpp", ""), address=values.get("address", ""),
                           edo_id=edo_id)
        found.extend(problem for problem in party.problems("продавца")
                     if problem not in found)
    return found + signer.problems


class SellerDialog(QDialog):
    """Реквизиты продавца и подписанта — один раз на рабочее место."""

    def __init__(self, values: dict[str, str], thumbprint: str, inn: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Продавец и подписант")
        self.setMinimumWidth(640)
        self._thumbprint = thumbprint
        layout = QVBoxLayout(self)
        layout.setSpacing(9)
        # Одна форма на всё окно: подписи в одну колонку, поля одной высоты.
        form = QFormLayout()
        form.setVerticalSpacing(8)
        self.edits: dict[str, QLineEdit] = {}

        def add(name: str, label: str, placeholder: str = "") -> QLineEdit:
            edit = QLineEdit(values.get(name, ""), self)
            edit.setPlaceholderText(placeholder)
            form.addRow(label, edit)
            self.edits[name] = edit
            return edit

        def hint(text: str) -> None:
            label = Hint(text, self)
            # В форме перенос строк считает высоту неверно и обрезает текст.
            label.setWordWrap(False)
            form.addRow("", label)

        form.addRow(SectionTitle("Продавец", self))
        add("inn", "ИНН", "10 цифр — организация, 12 — ИП")
        if not self.edits["inn"].text().strip():
            self.edits["inn"].setText(inn)
        add("name", "Название / ФИО ИП", "ООО «…» или Фамилия Имя Отчество")
        add("kpp", "КПП", "Для организации")
        add("address", "Адрес", "Из ЕГРЮЛ / ЕГРИП")
        add("diadoc_id", "ID в Диадоке", "2BM-…").setToolTip(
            "С ним сохраняется УПД для Диадока. Он же стоит в УПД, которые вам "
            "присылают поставщики через Диадок")
        hint("Где взять: Диадок → «Реквизиты организации»")
        add("edo_id", "ID в «ЭДО Лайт»", "2LT-…").setToolTip(
            "Нужен только для отправки из программы через «ЭДО Лайт». Покупатель из "
            "другого ЭДО должен быть связан с вами роумингом именно в «ЭДО Лайт».")
        hint("Где взять: «ЭДО Лайт» → профиль организации")

        form.addRow(SectionTitle("Подписант", self))
        add("surname", "Фамилия")
        add("first_name", "Имя")
        add("patronymic", "Отчество")
        add("position", "Должность", "Например, «Менеджер»; ИП — «Индивидуальный предприниматель»")
        self.from_certificate = QPushButton("Взять из сертификата", self)
        self.from_certificate.setIcon(icons.icon("certificate"))
        self.from_certificate.setEnabled(bool(thumbprint))
        self.from_certificate.clicked.connect(lambda _checked=False: self._fill_signer())
        form.addRow("", self.from_certificate)

        self.poa_box = QCheckBox("Подписывает по МЧД", self)
        self.poa_box.setToolTip("Подписант — не руководитель (не сам ИП): полномочия "
                                "подтверждает машиночитаемая доверенность")
        form.addRow("", self.poa_box)
        self.poa_rows = [add("poa_number", "Номер МЧД", "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"),
                         add("poa_date", "Дата выдачи", "ГГГГ-ММ-ДД"),
                         add("poa_system", "Где хранится", sale.POA_SYSTEM)]
        if not self.edits["poa_system"].text().strip():
            self.edits["poa_system"].setText(sale.POA_SYSTEM)
        self._form = form
        self.poa_box.setChecked(bool(values.get("poa_number", "").strip()))
        self.poa_box.toggled.connect(self._show_poa)
        self._show_poa(self.poa_box.isChecked())
        layout.addLayout(form)

        self.problem_label = QLabel("", self)
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet(f"color: {Palette.DANGER};")
        layout.addWidget(self.problem_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _show_poa(self, shown: bool) -> None:
        for edit in self.poa_rows:
            self._form.setRowVisible(edit, shown)

    def _fill_signer(self) -> None:
        certificate = crypto.find(self._thumbprint)
        if certificate is None:
            self.problem_label.setText("Сертификат не найден — подключите носитель ключа.")
            return
        surname, name, patronymic = certificate.person
        for field_name, value in (("surname", surname), ("first_name", name),
                                  ("patronymic", patronymic),
                                  ("position", certificate.position)):
            if value:
                self.edits[field_name].setText(value)

    def values(self) -> dict[str, str]:
        result = {name: " ".join(edit.text().split()) for name, edit in self.edits.items()}
        if not self.poa_box.isChecked():
            result.update(poa_number="", poa_date="")
        return result

    def _accept(self) -> None:
        values = self.values()
        problems = seller_problems(values)
        if self.poa_box.isChecked() and not values["poa_number"]:
            problems.insert(0, "укажите номер доверенности или снимите галочку")
        if problems:
            self.problem_label.setText("Не сохранено: " + "; ".join(problems) + ".")
            self.adjustSize()
            return
        self.accept()
