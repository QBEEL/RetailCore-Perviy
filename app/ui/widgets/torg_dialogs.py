"""Окна Торг-2: составление акта ТОРГ-2, реквизиты для бланка, справочник брендов."""
from __future__ import annotations

from datetime import date

from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QColor, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.claims import Brand, TorgAct, TorgItem, TorgSettings, brand_key, remote
from ...core.claims import torg
from ...core.payments.transport import ServerError
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle
from .inputs import NumberInput, SelectBox
from .payment_dialogs import DateInput

# Колонки таблицы товаров. Редактируются цена и четыре количества; остальное
# считается и менять его руками нельзя — иначе акт разошёлся бы сам с собой.
(COL_NAME, COL_BARCODE, COL_PRICE, COL_DOC, COL_SHORT, COL_DEFECT, COL_SURPLUS,
 COL_FACT, COL_SUM) = range(9)
_EDITABLE = (COL_PRICE, COL_DOC, COL_SHORT, COL_DEFECT, COL_SURPLUS)


ROW_HEIGHT = 38


class _NumberCellDelegate(QStyledItemDelegate):
    """Редактор числовой ячейки без тесных отступов темы.

    Стандартное поле ввода приложения рассчитано на форму: отступы 7 на 10
    пикселей. В строке таблицы оно не помещается, и вводимые цифры не видны.
    Тот же приём, что у таблицы заказа кодов.
    """

    def createEditor(self, parent, option, index):  # noqa: N802 — Qt
        editor = QLineEdit(parent)
        editor.setStyleSheet("QLineEdit { padding: 0 8px; margin: 0; }")
        editor.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # Цифры, пробел, запятая и точка: проверка числа — при вводе акта.
        editor.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9 .,]*"), editor))
        editor.setMaxLength(14)
        return editor

    def updateEditorGeometry(self, editor, option, index):  # noqa: N802 — Qt
        editor.setGeometry(option.rect)


def parse_number(text: str) -> float | None:
    """Число из ячейки: «375,5», «1 200», «12.50». Пусто — ноль, мусор — None."""
    cleaned = text.replace(" ", "").replace(" ", "").replace(",", ".").strip()
    if not cleaned:
        return 0.0
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value == value and abs(value) < 1e12 else None


def _text(value: float) -> str:
    """Число для ячейки: «36», «375,5» — без «.0» и с запятой, как пишут у нас."""
    if not value:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}".replace(".", ",")


def _money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _buttons(dialog: QDialog, ok_text: str) -> QDialogButtonBox:
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
    ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
    ok.setText(ok_text)
    ok.setObjectName("Primary")
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
    buttons.rejected.connect(dialog.reject)
    return buttons


def _error_label() -> QLabel:
    label = QLabel("")
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {Palette.DANGER};")
    label.hide()
    return label


# --- реквизиты ----------------------------------------------------------------

class TorgSettingsDialog(QDialog):
    """Реквизиты организации и состав комиссии — общие для всех актов отдела."""

    def __init__(self, settings: TorgSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Реквизиты ТОРГ-2")
        self.setMinimumWidth(640)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(Hint(
            "Эти данные подставляются в каждый акт. Они общие для всего отдела: "
            "изменив их здесь, вы меняете их у всех."))

        form = QFormLayout()
        form.setSpacing(Metrics.GAP)
        self.organization = QLineEdit(settings.organization)
        self.organization.setPlaceholderText("Название, адрес, телефон")
        form.addRow("Организация", self.organization)
        self.department = QLineEdit(settings.department)
        form.addRow("Подразделение", self.department)
        self.director_title = QLineEdit(settings.director_title)
        self.director_title.setPlaceholderText("в графе «Руководитель», например «ИП Иванов И. И.»")
        form.addRow("Руководитель", self.director_title)
        self.director_name = QLineEdit(settings.director_name)
        self.director_name.setPlaceholderText("Фамилия И. О.")
        form.addRow("Расшифровка подписи", self.director_name)
        self.place = QLineEdit(settings.place)
        self.place.setPlaceholderText("адрес склада")
        form.addRow("Место приёмки", self.place)
        root.addLayout(form)

        root.addWidget(SectionTitle("Комиссия"))
        commission = QFormLayout()
        commission.setSpacing(Metrics.GAP)
        self.chair_title = QLineEdit(settings.chair_title)
        self.chair_name = QLineEdit(settings.chair_name)
        row = QHBoxLayout()
        row.addWidget(self.chair_title, 1)
        row.addWidget(self.chair_name, 1)
        commission.addRow("Председатель", row)

        self._members: list[tuple[QLineEdit, QLineEdit]] = []
        for index in range(torg.MAX_MEMBERS):
            title, name = settings.members[index] if index < len(settings.members) else ("", "")
            title_edit, name_edit = QLineEdit(title), QLineEdit(name)
            title_edit.setPlaceholderText("должность")
            name_edit.setPlaceholderText("Фамилия И.")
            line = QHBoxLayout()
            line.addWidget(title_edit, 1)
            line.addWidget(name_edit, 1)
            commission.addRow(f"Член комиссии {index + 1}", line)
            self._members.append((title_edit, name_edit))
        root.addLayout(commission)

        self.error = _error_label()
        root.addWidget(self.error)
        buttons = _buttons(self, "Сохранить")
        buttons.accepted.connect(self._accept)
        root.addWidget(buttons)

    def value(self) -> TorgSettings:
        return TorgSettings(
            organization=self.organization.text().strip(),
            department=self.department.text().strip(),
            director_title=self.director_title.text().strip(),
            director_name=self.director_name.text().strip(),
            place=self.place.text().strip(),
            chair_title=self.chair_title.text().strip(),
            chair_name=self.chair_name.text().strip(),
            members=[(t.text().strip(), n.text().strip()) for t, n in self._members
                     if t.text().strip() or n.text().strip()])

    def _accept(self) -> None:
        if missing := self.value().problems():
            self.error.setText("Заполните: " + ", ".join(missing))
            self.error.show()
            return
        self.accept()


# --- справочник брендов -------------------------------------------------------

class BrandsDialog(QDialog):
    """Список брендов: поиск, добавление и — администратору — удаление.

    Список `brands` меняется на месте, поэтому страница видит правки сразу, не
    перечитывая сервер.
    """

    def __init__(self, brands: list[Brand], can_delete: bool,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.brands = brands
        self.setWindowTitle("Бренды")
        self.setMinimumSize(460, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(Hint(
            "По этому списку программа подсказывает бренд, когда вы начинаете его "
            "печатать в заявке. Нужного нет — добавьте."))

        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Поиск по списку")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refill)
        root.addWidget(self.search)

        self.list = QListWidget(self)
        root.addWidget(self.list, 1)

        add_row = QHBoxLayout()
        self.new_name = QLineEdit(self)
        self.new_name.setPlaceholderText("Новый бренд")
        self.new_name.returnPressed.connect(self._add)
        add_row.addWidget(self.new_name, 1)
        self.add_button = QPushButton("Добавить", self)
        self.add_button.setObjectName("Primary")
        self.add_button.clicked.connect(self._add)
        add_row.addWidget(self.add_button)
        self.delete_button = QPushButton("Убрать", self)
        self.delete_button.setObjectName("Danger")
        self.delete_button.setVisible(can_delete)
        self.delete_button.clicked.connect(self._delete)
        add_row.addWidget(self.delete_button)
        root.addLayout(add_row)

        self.error = _error_label()
        root.addWidget(self.error)
        self.count = Hint("", self)
        root.addWidget(self.count)
        close = QPushButton("Закрыть", self)
        close.clicked.connect(self.accept)
        root.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self._refill()

    def _refill(self) -> None:
        wanted = brand_key(self.search.text())
        self.list.clear()
        for brand in sorted(self.brands, key=lambda b: brand_key(b.name)):
            if wanted and wanted not in brand_key(brand.name):
                continue
            self.list.addItem(brand.name)
            self.list.item(self.list.count() - 1).setData(Qt.ItemDataRole.UserRole, brand.id)
        self.count.setText(f"Брендов в списке: {len(self.brands)}")

    def _fail(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()

    def _add(self) -> None:
        name = " ".join(self.new_name.text().split())
        if not name:
            return
        self.error.hide()
        try:
            brand = remote.add_brand(name)
        except ServerError as failure:
            self._fail(str(failure))
            return
        if all(b.id != brand.id for b in self.brands):
            self.brands.append(brand)
        self.new_name.clear()
        self.search.setText(brand.name)
        self._refill()

    def _delete(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        brand_id = int(item.data(Qt.ItemDataRole.UserRole))
        try:
            remote.delete_brand(brand_id)
        except ServerError as failure:
            self._fail(str(failure))
            return
        self.brands[:] = [b for b in self.brands if b.id != brand_id]
        self._refill()


# --- акт ----------------------------------------------------------------------

class TorgDialog(QDialog):
    """Составление акта ТОРГ-2.

    Склад заполняет то, чего нет в заявках: поставщика и документы, цены и
    количества по документам. Остальное — товар, штрих-код, недостача и брак —
    берётся из заявок, а суммы считаются сами.
    """

    def __init__(self, act: TorgAct, settings: TorgSettings, suppliers: list[dict],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.act = act
        self.settings = settings
        # Реквизиты правили в этом окне — страница сохранит их на сервере.
        self.settings_changed = False
        self._suppliers = {s["supplier"]: s for s in suppliers}
        self._items = [TorgItem(**i.to_dict()) for i in act.items]
        self.setWindowTitle(f"Акт ТОРГ-2 №{act.number}" if act.id else "Новый акт ТОРГ-2")
        self.setMinimumSize(980, 720)
        self._build()
        self._fill_table()
        self._refresh_totals()

    # --- разметка -------------------------------------------------------------

    def _build(self) -> None:
        act = self.act
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        top = QHBoxLayout()
        top.setSpacing(Metrics.GAP * 2)

        left = QFormLayout()
        left.setSpacing(Metrics.GAP)
        self.number = NumberInput(self)
        self.number.setRange(1, 999_999)
        self.number.setValue(act.number or 1)
        self.number.setMaximumWidth(120)
        left.addRow("Номер акта", self.number)
        self.act_date = DateInput(self)
        self.act_date.set_value(act.act_date)
        left.addRow("Дата акта", self.act_date)
        self.receipt_date = DateInput(self)
        self.receipt_date.set_value(act.receipt_date)
        self.receipt_date.setToolTip("Когда товар приняли на склад")
        left.addRow("Дата приёмки", self.receipt_date)
        top.addLayout(left)

        right = QFormLayout()
        right.setSpacing(Metrics.GAP)
        self.supplier = SelectBox(self)
        self.supplier.setEditable(True)
        self.supplier.setInsertPolicy(SelectBox.InsertPolicy.NoInsert)
        self.supplier.addItems(list(self._suppliers))
        self.supplier.setCurrentText(act.supplier)
        self.supplier.lineEdit().setPlaceholderText("Название, адрес, телефон поставщика")
        self.supplier.activated.connect(self._pick_supplier)
        right.addRow("Поставщик", self.supplier)

        contract = QHBoxLayout()
        self.contract_number = QLineEdit(act.contract_number)
        self.contract_number.setPlaceholderText("№")
        self.contract_date = DateInput(self)
        self.contract_date.set_value(act.contract_date)
        contract.addWidget(self.contract_number, 1)
        contract.addWidget(QLabel("от"))
        contract.addWidget(self.contract_date)
        right.addRow("Договор", contract)

        invoice = QHBoxLayout()
        self.invoice_number = QLineEdit(act.invoice_number)
        self.invoice_number.setPlaceholderText("№")
        self.invoice_date = DateInput(self)
        self.invoice_date.set_value(act.invoice_date)
        invoice.addWidget(self.invoice_number, 1)
        invoice.addWidget(QLabel("от"))
        invoice.addWidget(self.invoice_date)
        right.addRow("Счёт-фактура", invoice)

        delivery = QHBoxLayout()
        self.delivery = QLineEdit(act.delivery)
        self.delivery.setPlaceholderText("например, Деловые линии")
        self.waybill = QLineEdit(act.waybill)
        self.waybill.setPlaceholderText("№ накладной")
        delivery.addWidget(self.delivery, 2)
        delivery.addWidget(self.waybill, 1)
        right.addRow("Доставка", delivery)
        top.addLayout(right, 1)
        root.addLayout(top)

        root.addWidget(SectionTitle("Товары"))
        root.addWidget(Hint(
            "Цену и количество по документам возьмите из счёта-фактуры. Недостача, "
            "брак и излишек пришли из заявок — при необходимости поправьте. Брак "
            "входит в «Поступило»: он на складе, просто негодный; излишек поступил "
            "сверх документов и тоже входит."))
        self.table = QTableWidget(0, 9, self)
        self.table.setHorizontalHeaderLabels(
            ["Товар", "Штрих-код", "Цена, ₽", "По документам", "Недостача", "Брак",
             "Излишек", "Поступило", "Претензия, ₽"])
        header = self.table.horizontalHeader()
        # Название тянется на всё свободное место, остальные колонки — узкие и
        # одной ширины: по содержимому они «прыгали» бы при каждом вводе цифры.
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(60)
        for col, width in ((COL_BARCODE, 130), (COL_PRICE, 80), (COL_DOC, 115),
                           (COL_SHORT, 90), (COL_DEFECT, 70), (COL_SURPLUS, 80),
                           (COL_FACT, 95), (COL_SUM, 115)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(col, width)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self._number_delegate = _NumberCellDelegate(self.table)
        for col in _EDITABLE:
            self.table.setItemDelegateForColumn(col, self._number_delegate)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setMinimumHeight(160)
        self.table.itemChanged.connect(self._cell_changed)
        root.addWidget(self.table, 1)
        self.totals = Hint("", self)
        root.addWidget(self.totals)

        root.addWidget(SectionTitle("Описание и заключение"))
        self.description = QPlainTextEdit(act.description, self)
        self.description.setPlaceholderText("Подробное описание дефектов и причин")
        self.description.setFixedHeight(64)
        self.description.textChanged.connect(self._check_text)
        root.addWidget(self.description)
        self.conclusion = QPlainTextEdit(act.conclusion, self)
        self.conclusion.setPlaceholderText("Заключение комиссии")
        self.conclusion.setFixedHeight(48)
        self.conclusion.textChanged.connect(self._check_text)
        root.addWidget(self.conclusion)
        self.text_hint = Hint("", self)
        root.addWidget(self.text_hint)

        self.error = _error_label()
        root.addWidget(self.error)

        bottom = QHBoxLayout()
        self.settings_button = QPushButton("Реквизиты…", self)
        self.settings_button.setToolTip("Организация, руководитель и комиссия — общие для всех актов")
        self.settings_button.clicked.connect(self._edit_settings)
        bottom.addWidget(self.settings_button)
        bottom.addStretch(1)
        buttons = _buttons(self, "Составить акт")
        buttons.accepted.connect(self._accept)
        bottom.addWidget(buttons)
        root.addLayout(bottom)

        if not act.description and not act.conclusion:
            self.description.setPlainText(act.suggested_text()[0])
            self.conclusion.setPlainText(act.suggested_text()[1])
        self._check_text()

    # --- таблица --------------------------------------------------------------

    def _fill_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._items))
        for row, item in enumerate(self._items):
            self._set_cell(row, COL_NAME, item.name, editable=False)
            self._set_cell(row, COL_BARCODE, item.barcode, editable=False)
            self._set_cell(row, COL_PRICE, _text(item.price), editable=True)
            self._set_cell(row, COL_DOC, _text(item.qty_doc), editable=True)
            self._set_cell(row, COL_SHORT, _text(item.qty_short), editable=True)
            self._set_cell(row, COL_DEFECT, _text(item.qty_defect), editable=True)
            self._set_cell(row, COL_SURPLUS, _text(item.qty_surplus), editable=True)
            self._set_cell(row, COL_FACT, "", editable=False)
            self._set_cell(row, COL_SUM, "", editable=False)
            self._recalc_row(row)
        self.table.blockSignals(False)

    def _set_cell(self, row: int, col: int, text: str, *, editable: bool) -> None:
        cell = QTableWidgetItem(text)
        flags = cell.flags()
        cell.setFlags(flags | Qt.ItemFlag.ItemIsEditable if editable
                      else flags & ~Qt.ItemFlag.ItemIsEditable)
        if col == COL_NAME:
            # Название в узкой колонке обрезается; целиком оно в подсказке.
            cell.setToolTip(text)
        if col >= COL_PRICE:
            cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if not editable:
            cell.setForeground(QColor(Palette.TEXT_MUTED if col >= COL_FACT else Palette.TEXT))
        self.table.setItem(row, col, cell)

    def _cell_changed(self, cell: QTableWidgetItem) -> None:
        row, col = cell.row(), cell.column()
        if col not in _EDITABLE:
            return
        value = parse_number(cell.text())
        self.table.blockSignals(True)
        if value is None or value < 0:
            # Мусор в ячейке не прячем: красим и считаем нулём, а акт без
            # исправления не составится.
            cell.setBackground(QColor(Palette.DANGER_SOFT))
            value = 0.0
        else:
            cell.setBackground(QColor(0, 0, 0, 0))
        self._store(row, col, value)
        self._recalc_row(row)
        self.table.blockSignals(False)
        self._refresh_totals()

    def _store(self, row: int, col: int, value: float) -> None:
        item = self._items[row]
        if col == COL_PRICE:
            item.price = value
        elif col == COL_DOC:
            item.qty_doc = value
        elif col == COL_SHORT:
            item.qty_short = value
        elif col == COL_DEFECT:
            item.qty_defect = value
        elif col == COL_SURPLUS:
            item.qty_surplus = value

    def _recalc_row(self, row: int) -> None:
        item = self._items[row]
        fact = self.table.item(row, COL_FACT)
        claim = self.table.item(row, COL_SUM)
        # Пока количество по документам не введено, «поступило» посчитать нечем:
        # вместо «-5» (недостача минус ноль) ячейка остаётся пустой.
        fact.setText((_text(item.qty_fact) or "0") if item.qty_doc or item.qty_surplus else "")
        claim.setText(_money(item.sum_short + item.sum_defect))

    def _refresh_totals(self) -> None:
        totals = self.act_snapshot().totals()
        self.totals.setText(
            f"По документам: {_text(totals['qty_doc']) or 0} шт на {_money(totals['sum_doc'])} ₽ · "
            f"недостача: {_text(totals['qty_short']) or 0} шт на {_money(totals['sum_short'])} ₽ · "
            f"брак: {_text(totals['qty_defect']) or 0} шт на {_money(totals['sum_defect'])} ₽"
            + (f" · излишек: {_text(totals['qty_surplus'])} шт на {_money(totals['sum_surplus'])} ₽"
               if totals["qty_surplus"] else ""))

    # --- поставщик ------------------------------------------------------------

    def _pick_supplier(self, index: int) -> None:
        """Выбрал поставщика из прошлых актов — договор и доставка подставляются.

        Только в пустые поля: набранное руками важнее памяти.
        """
        hint = self._suppliers.get(self.supplier.itemText(index))
        if not hint:
            return
        if not self.contract_number.text().strip():
            self.contract_number.setText(hint.get("contract_number", ""))
        if self.contract_date.value() is None and hint.get("contract_date"):
            try:
                self.contract_date.set_value(date.fromisoformat(hint["contract_date"]))
            except ValueError:
                pass
        if not self.delivery.text().strip():
            self.delivery.setText(hint.get("delivery", ""))

    # --- текст ----------------------------------------------------------------

    def _check_text(self) -> None:
        bad = []
        if torg.overflow(self.description.toPlainText(), torg.DESCRIPTION_LINES):
            bad.append("описание")
        if torg.overflow(self.conclusion.toPlainText(), torg.CONCLUSION_LINES):
            bad.append("заключение")
        if bad:
            self.text_hint.setText("Не помещается в бланк: " + " и ".join(bad) + " — сократите.")
            self.text_hint.setStyleSheet(f"color: {Palette.DANGER};")
        else:
            self.text_hint.setText(
                "Описание занимает до 4 строк бланка, заключение — до 3.")
            self.text_hint.setStyleSheet("")

    # --- реквизиты ------------------------------------------------------------

    def _edit_settings(self) -> None:
        dialog = TorgSettingsDialog(self.settings, self)
        if dialog.exec():
            self.settings = dialog.value()
            self.settings_changed = True

    # --- результат ------------------------------------------------------------

    def act_snapshot(self) -> TorgAct:
        """Акт по тому, что сейчас в окне. Номер записи и заявки сохраняются."""
        act = self.act
        return TorgAct(
            id=act.id, number=int(self.number.value()),
            act_date=self.act_date.value() or act.act_date,
            receipt_date=self.receipt_date.value() or act.receipt_date,
            supplier=" ".join(self.supplier.currentText().split()),
            contract_number=self.contract_number.text().strip(),
            contract_date=self.contract_date.value(),
            invoice_number=self.invoice_number.text().strip(),
            invoice_date=self.invoice_date.value(),
            delivery=self.delivery.text().strip(), waybill=self.waybill.text().strip(),
            description=self.description.toPlainText().strip(),
            conclusion=self.conclusion.toPlainText().strip(),
            items=[TorgItem(**item.to_dict()) for item in self._items])

    def _accept(self) -> None:
        problems = []
        if missing := self.settings.problems():
            problems.append("Заполните реквизиты (кнопка «Реквизиты…»): " + ", ".join(missing))
        # Дата, набранная с ошибкой, иначе молча заменилась бы датой акта.
        for field, title in ((self.act_date, "дата акта"), (self.receipt_date, "дата приёмки"),
                             (self.contract_date, "дата договора"),
                             (self.invoice_date, "дата счёта-фактуры")):
            if field.text().strip() and field.value() is None:
                problems.append(f"Неверная {title}: нужен вид дд.мм.гггг")
        for row in range(self.table.rowCount()):
            for col in _EDITABLE:
                cell = self.table.item(row, col)
                if cell and (parse_number(cell.text()) is None
                             or (parse_number(cell.text()) or 0) < 0):
                    problems.append(f"Строка {row + 1}: в ячейке не число")
                    break
        if not problems:
            problems = self.act_snapshot().problems()
        if problems:
            self.error.setText("\n".join(problems[:6]))
            self.error.show()
            return
        self.accept()
