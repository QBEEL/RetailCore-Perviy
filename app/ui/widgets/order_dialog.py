"""Окно «Новый заказ кодов»: много товаров с разными GTIN за один раз.

Раньше заказ был карточкой на странице: таблица в полторы строки, а цифры в ячейке
при правке отрисовывались мелко — ошибиться в коде товара было проще простого.
Теперь это отдельное окно, и устроено оно под заказ в десятки позиций:

- таблица занимает окно, шрифт крупный, редактор ячейки тоже;
- список товаров вставляется целиком из Excel, письма или 1С — по строке на
  товар, «код — количество»;
- всё, что СУЗ отвергла бы, видно в подсказке до отправки.

Окно ничего не отправляет: оно собирает `Request`. Подтверждение и отправка — на
странице, как и прежде.
"""
from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIntValidator, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.marking import GROUPS, orders as orders_module
from ...core.marking.orders import ReleaseMethod
from .. import icons
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle

# Крупнее обычного: в этой таблице проверяют числа глазами, и ошибка в одной
# цифре кода товара — это заказ не на тот товар.
TABLE_FONT_PT = 13
ROW_HEIGHT = 40


class _EditorDelegate(QStyledItemDelegate):
    """Редактор ячейки того же крупного размера, что и сама таблица.

    Без него при правке открывался стандартный редактор приложения с мелким
    шрифтом и тесными полями — цифры в нём едва читались.
    """

    def __init__(self, digits_only: bool, parent=None) -> None:
        super().__init__(parent)
        self._digits_only = digits_only

    def createEditor(self, parent, option, index):  # noqa: N802 — Qt
        editor = QLineEdit(parent)
        font = QFont(editor.font())
        font.setPointSize(TABLE_FONT_PT + 1)
        editor.setFont(font)
        editor.setStyleSheet("QLineEdit { padding: 0 8px; margin: 0; }")
        if self._digits_only:
            editor.setValidator(QIntValidator(0, orders_module.MAX_QUANTITY, editor))
            editor.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        else:
            editor.setMaxLength(14)
        return editor

    def updateEditorGeometry(self, editor, option, index):  # noqa: N802 — Qt
        editor.setGeometry(option.rect)


class OrderDialog(QDialog):
    """Форма заказа. Результат — `request`, если окно закрыто кнопкой «Заказать»."""

    def __init__(self, known_orders: Sequence, has_certificate: bool,
                 contact: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Новый заказ кодов")
        self.setModal(True)
        self.resize(940, 700)
        self.setMinimumSize(720, 520)
        self._orders = list(known_orders)
        self._has_certificate = has_certificate
        self._build()
        self.contact_edit.setText(contact)
        self._sync_template()
        self._sync_hint()

    # --- разметка -----------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle("Новый заказ кодов"))

        chooser = QHBoxLayout()
        chooser.setSpacing(9)
        self.group_box = QComboBox(self)
        for group in GROUPS:
            self.group_box.addItem(group.title, group.code)
        chooser.addWidget(self.group_box, 1)
        self.method_box = QComboBox(self)
        for method in ReleaseMethod:
            self.method_box.addItem(method.title, method.value)
        chooser.addWidget(self.method_box, 1)
        root.addLayout(chooser)

        details = QFormLayout()
        details.setSpacing(9)
        self.contact_edit = QLineEdit(self)
        self.contact_edit.setPlaceholderText("Кому в ЦРПТ писать по этому заказу")
        details.addRow("Контактное лицо", self.contact_edit)

        # Шаблон выбирается из подходящих группе — списком, а не числом: номер сам
        # по себе не значит для человека ничего. Различаются шаблоны длиной
        # серийного номера и наличием криптохвоста, это и написано в каждой строке.
        self.template_box = QComboBox(self)
        details.addRow("Шаблон кода", self.template_box)

        payment = QHBoxLayout()
        payment.setSpacing(9)
        self.payment_box = QSpinBox(self)
        self.payment_box.setRange(1, 9)
        self.payment_box.setValue(orders_module.DEFAULT_PAYMENT)
        payment.addWidget(self.payment_box)
        self.template_hint = Hint("", self)
        payment.addWidget(self.template_hint, 1)
        details.addRow("Способ оплаты", payment)
        root.addLayout(details)

        bar = QHBoxLayout()
        bar.setSpacing(9)
        bar.addWidget(SectionTitle("Товары"))
        bar.addStretch(1)
        self.paste_button = self._button("Вставить список", "copy", self.paste_lines)
        self.paste_button.setToolTip(
            "Строки «код товара — количество» из буфера обмена: Excel, письмо, 1С. "
            "Штрихкод из 13 цифр дополняется до GTIN-14, одинаковые коды складываются")
        bar.addWidget(self.paste_button)
        bar.addWidget(self._button("Добавить строку", "plus", self.add_line))
        bar.addWidget(self._button("Убрать", "trash", self.remove_line))
        root.addLayout(bar)

        self.lines = QTableWidget(0, 2, self)
        self.lines.setHorizontalHeaderLabels(["Код товара (GTIN)", "Количество"])
        self.lines.verticalHeader().setVisible(False)
        self.lines.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.lines.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.lines.setAlternatingRowColors(True)
        font = QFont(self.lines.font())
        font.setPointSize(TABLE_FONT_PT)
        self.lines.setFont(font)
        self.lines.setItemDelegateForColumn(0, _EditorDelegate(False, self.lines))
        self.lines.setItemDelegateForColumn(1, _EditorDelegate(True, self.lines))
        head = self.lines.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.lines.setColumnWidth(1, 200)
        root.addWidget(self.lines, 1)
        # Ctrl+V в таблице — тот же список, что и кнопка: руки уже на клавиатуре.
        QShortcut(QKeySequence.StandardKey.Paste, self.lines, self.paste_lines,
                  context=Qt.ShortcutContext.WidgetShortcut)

        self.hint = Hint("", self)
        root.addWidget(self.hint)

        row = QHBoxLayout()
        row.setSpacing(9)
        row.addStretch(1)
        self.order_button = QPushButton("Заказать коды", self)
        self.order_button.setObjectName("Primary")
        self.order_button.setIcon(icons.icon("run"))
        # Не кнопка по умолчанию: Enter, нажатый при правке ячейки, не должен
        # отправлять заказ.
        self.order_button.setAutoDefault(False)
        self.order_button.clicked.connect(self._accept_order)
        cancel = QPushButton("Отмена", self)
        cancel.setDefault(True)
        cancel.clicked.connect(self.reject)
        row.addWidget(self.order_button)
        row.addWidget(cancel)
        root.addLayout(row)

        self.group_box.currentIndexChanged.connect(self._on_group_changed)
        self.method_box.currentIndexChanged.connect(self._sync_hint)
        self.template_box.currentIndexChanged.connect(self._sync_hint)
        self.payment_box.valueChanged.connect(self._sync_hint)
        self.contact_edit.textChanged.connect(self._sync_hint)
        self.lines.itemChanged.connect(self._sync_hint)

    def _button(self, title: str, icon: str, handler) -> QPushButton:
        button = QPushButton(title, self)
        button.setIcon(icons.icon(icon))
        button.setAutoDefault(False)
        button.clicked.connect(handler)
        return button

    # --- заказ ---------------------------------------------------------------------

    @property
    def request(self) -> orders_module.Request:
        """Заказ в том виде, в каком он уйдёт в СУЗ."""
        lines = []
        for row in range(self.lines.rowCount()):
            gtin = self.lines.item(row, 0)
            quantity = self.lines.item(row, 1)
            lines.append(orders_module.Line(
                gtin=gtin.text().strip() if gtin else "",
                quantity=_number(quantity.text() if quantity else ""),
                template_id=self._template_id(),
            ))
        return orders_module.Request(
            product_group=str(self.group_box.currentData() or ""),
            method=ReleaseMethod(str(self.method_box.currentData() or "REMAINS")),
            contact=self.contact_edit.text(),
            payment_type=self.payment_box.value(),
            lines=lines,
            seen_templates=self._seen_templates(),
        )

    def _seen_templates(self) -> frozenset[int]:
        """Шаблоны, которыми эту группу уже заказывали.

        Справочник может отстать от системы, а принятый ею шаблон — довод
        сильнее руководства.
        """
        group = str(self.group_box.currentData() or "").strip().lower()
        return frozenset(
            int(buffer.raw.get("templateId") or 0)
            for order in self._orders
            if order.product_group.strip().lower() == group
            for buffer in order.buffers
            if buffer.raw.get("templateId")
        )

    def _template_id(self) -> int:
        """Выбранный шаблон. У незнакомой группы список правится руками."""
        chosen = self.template_box.currentData()
        if chosen is not None:
            return int(chosen)
        return _number(self.template_box.currentText())

    def _on_group_changed(self) -> None:
        self._sync_template()
        self._sync_hint()

    def _sync_template(self) -> None:
        """Заполняет список шаблонов группы и выбирает подходящий.

        Основа — справочник из руководства СУЗ: у каждой товарной группы свой
        набор, и чужой шаблон система отвергает. История заказов уточняет выбор
        там, где шаблонов у группы несколько.
        """
        group = str(self.group_box.currentData() or "")
        known = orders_module.defaults_for(group, self._orders)
        allowed = orders_module.templates_for(group)

        self.template_box.blockSignals(True)
        self.template_box.clear()
        for template in allowed:
            self.template_box.addItem(template.title, template.id)
        if not allowed:
            # Группы нет в справочнике — запрещать нечем, но и подсказать
            # нечего: номер придётся взять из личного кабинета.
            self.template_box.setEditable(True)
            self.template_box.addItem(str(known.template_id or ""),
                                      known.template_id)
        else:
            self.template_box.setEditable(False)
            if known.known and self.template_box.findData(known.template_id) < 0:
                # Справочник этого шаблона не знает, а СУЗ его когда-то приняла.
                # Реальность старше документа — показываем оба.
                self.template_box.insertItem(
                    0, f"{known.template_id} · из вашего заказа по этой группе",
                    known.template_id)
            if (index := self.template_box.findData(known.template_id)) >= 0:
                self.template_box.setCurrentIndex(index)
        self.template_box.blockSignals(False)

        self.payment_box.blockSignals(True)
        self.payment_box.setValue(known.payment_type)
        self.payment_box.blockSignals(False)

        if known.known:
            when = f" от {known.since:%d.%m.%Y}" if known.since else ""
            self.template_hint.setStyleSheet("")
            self.template_hint.setText(
                f"Как в вашем заказе{when} по этой группе — менять не нужно")
            return
        self.template_hint.setStyleSheet("" if allowed
                                         else f"color: {Palette.WARNING};")
        self.template_hint.setText(
            "Шаблон — из справочника СУЗ для этой товарной группы"
            if allowed else
            "Этой группы нет в справочнике шаблонов — номер возьмите в личном "
            "кабинете СУЗ")

    # --- строки --------------------------------------------------------------------

    def add_line(self, gtin: str = "", quantity: int | str = 0) -> None:
        self.lines.blockSignals(True)
        row = self.lines.rowCount()
        self.lines.insertRow(row)
        self.lines.setItem(row, 0, QTableWidgetItem(str(gtin)))
        cell = QTableWidgetItem(str(quantity))
        cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lines.setItem(row, 1, cell)
        self.lines.blockSignals(False)
        self._sync_hint()
        if not gtin:
            self.lines.setCurrentCell(row, 0)
            self.lines.editItem(self.lines.item(row, 0))

    def remove_line(self) -> None:
        rows = sorted({index.row() for index in self.lines.selectedIndexes()},
                      reverse=True)
        if not rows and self.lines.currentRow() >= 0:
            rows = [self.lines.currentRow()]
        for row in rows:
            self.lines.removeRow(row)
        self._sync_hint()

    def paste_lines(self) -> None:
        """Добавляет строки из буфера обмена: «код товара — количество»."""
        found = orders_module.parse_lines(QApplication.clipboard().text())
        if not found.lines:
            self._warn("В буфере обмена нет строк вида «код товара — количество». "
                       "Скопируйте две колонки из Excel или по строке на товар.")
            return
        known = {self.lines.item(row, 0).text().strip(): row
                 for row in range(self.lines.rowCount())
                 if self.lines.item(row, 0)}
        added = 0
        for gtin, quantity in found.lines:
            if gtin in known:
                # Такой товар уже в таблице: количество прибавляется, а не
                # заводится второй строкой — одинаковые коды СУЗ не примет.
                row = known[gtin]
                current = _number(self.lines.item(row, 1).text())
                self.lines.item(row, 1).setText(str(current + quantity))
            else:
                self.add_line(gtin, quantity)
                added += 1
        self._sync_hint()
        notes = [f"Добавлено строк: {added}"]
        if found.merged:
            notes.append(f"одинаковых кодов сложено: {found.merged}")
        if found.skipped:
            notes.append(f"не разобрано строк: {len(found.skipped)} "
                         f"(первая: «{found.skipped[0][:40]}»)")
        self.hint.setStyleSheet(f"color: {Palette.WARNING};" if found.skipped else "")
        self.hint.setText(". ".join(notes) + ".")

    # --- подсказка и подтверждение ---------------------------------------------------

    def _problems(self) -> list[str]:
        problems = list(self.request.problems)
        if not self._has_certificate:
            problems.append("не выбран сертификат, а заказ подписывается")
        return problems

    def _sync_hint(self, *_args) -> None:
        request = self.request
        problems = self._problems()
        self.order_button.setEnabled(not problems)
        if problems:
            self.hint.setStyleSheet(f"color: {Palette.WARNING};")
            self.hint.setText("Пока нельзя отправить: " + "; ".join(problems) + ".")
            return
        self.hint.setStyleSheet("")
        self.hint.setText(
            f"К заказу: {_plural(len(request.lines), 'товар', 'товара', 'товаров')}, "
            f"{_plural(request.total, 'код', 'кода', 'кодов')}. При отправке "
            "КриптоПро попросит пароль к контейнеру — заказ подписывается. "
            "Отменить созданный заказ нельзя.")

    def _warn(self, text: str) -> None:
        self.hint.setStyleSheet(f"color: {Palette.WARNING};")
        self.hint.setText(text)

    def _accept_order(self) -> None:
        if self._problems():
            self._sync_hint()
            return
        self.accept()


def _number(text: str) -> int:
    digits = "".join(char for char in str(text) if char.isdigit())
    return int(digits) if digits else 0


def _plural(count: int, one: str, few: str, many: str) -> str:
    n = abs(count) % 100
    if 11 <= n <= 14:
        word = many
    elif n % 10 == 1:
        word = one
    elif 2 <= n % 10 <= 4:
        word = few
    else:
        word = many
    return f"{count} {word}"
