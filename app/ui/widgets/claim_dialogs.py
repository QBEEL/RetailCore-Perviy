"""Окна Торг-2: заявка, закрытие."""
from __future__ import annotations

import os
from typing import Any

from PySide6.QtCore import QStringListModel, Qt
from PySide6.QtWidgets import (
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.claims import (
    KIND_TITLES,
    RESOLUTION_TITLES,
    Brand,
    Claim,
    ClaimHint,
    Manager,
    brand_key,
    has_brand,
    remote,
)
from ...core.payments.safe_files import is_risky
from ...core.payments.transport import ServerError
from ..tasks import run_task
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle
from .inputs import DecimalInput, SelectBox
from .payment_dialogs import DateInput

# Менеджер не выбран. Заявка без КМ допустима: склад не всегда знает, чей это
# бренд, и назначить менеджера потом должен мочь любой.
NO_MANAGER = "— не назначен —"


def _buttons(dialog: QDialog, ok_text: str, *, ok_enabled: bool = True) -> QDialogButtonBox:
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
    ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
    ok.setText(ok_text)
    ok.setObjectName("Primary")
    ok.setEnabled(ok_enabled)
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
        "Отмена" if ok_enabled else "Закрыть")
    buttons.rejected.connect(dialog.reject)
    return buttons


class ClaimDialog(QDialog):
    """Заявка: новая или уже заведённая.

    Для новой есть список файлов, которые уйдут вместе с ней, — склад обычно
    держит ТОРГ-2 под рукой в момент заведения. Для заведённой файлы
    добавляются и убираются сразу, на сервере.

    Закрытая заявка и заявка, которую человек не вправе править, открываются
    только для чтения: так видно всё, но нельзя испортить чужое.
    """

    def __init__(self, claim: Claim | None, managers: list[Manager],
                 brands: list[Brand] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.claim = claim
        self._managers = managers
        self._brands = brands or []
        # Какие из набранных брендов человек попросил добавить в справочник.
        # Решает окно, а добавляет страница: окно ничего не пишет на сервер само.
        self.new_brands: list[str] = []
        # Позиции новой заявки, уже внесённые в список. Поставка может быть с
        # несколькими бракованными позициями: каждая станет отдельной заявкой.
        self._positions: list[dict[str, Any]] = []
        self._readonly = claim is not None and not claim.can_edit
        # Файлы новой заявки: пути на диске, ещё не отправленные.
        self.staged: list[str] = []
        self.setWindowTitle("Новая заявка" if claim is None else "Заявка Торг-2")
        self.setMinimumWidth(600)
        self._build()
        if claim is not None:
            self._fill(claim)
            self._reload_files()

    # --- разметка -------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        form = QFormLayout()
        form.setSpacing(Metrics.GAP)

        self.kind = SelectBox(self)
        for code, title in KIND_TITLES.items():
            self.kind.addItem(title, code)
        form.addRow("Что случилось", self.kind)

        self.barcode = QLineEdit(self)
        self.barcode.setPlaceholderText("отсканируйте или введите — остальное подставится")
        self.barcode.editingFinished.connect(self._lookup)
        form.addRow("Штрих-код", self.barcode)

        self.brand = QLineEdit(self)
        self.brand.setPlaceholderText("начните печатать — программа подскажет из списка")
        self._brand_model = QStringListModel([b.name for b in self._brands], self)
        completer = QCompleter(self._brand_model, self)
        # Совпадение по любому месту названия, а не только по началу: бренд
        # помнят то по первому слову, то по последнему.
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setMaxVisibleItems(10)
        self.brand.setCompleter(completer)
        self.brand.textChanged.connect(self._brand_typed)
        self.brand.editingFinished.connect(self._brand_done)
        form.addRow("Бренд", self.brand)
        self.brand_note = Hint("", self)
        self.brand_note.hide()
        form.addRow("", self.brand_note)

        self.product = QLineEdit(self)
        form.addRow("Номенклатура", self.product)

        self.spec = QLineEdit(self)
        self.spec.setPlaceholderText("объём, оттенок")
        form.addRow("Характеристика", self.spec)

        self.quantity = DecimalInput(self)
        self.quantity.setDecimals(2)
        self.quantity.setRange(0.01, 1_000_000)
        self.quantity.setValue(1)
        self.quantity.setMaximumWidth(140)
        form.addRow("Количество", self.quantity)

        self.expiry = DateInput(self)
        form.addRow("Срок годности", self.expiry)
        root.addLayout(form)

        if self.claim is None:
            self._build_positions(root)

        # Общее для всей поставки: КМ и комментарий вводятся один раз.
        common = QFormLayout()
        common.setSpacing(Metrics.GAP)
        self.manager = SelectBox(self)
        self.manager.addItem(NO_MANAGER, None)
        for manager in self._managers:
            self.manager.addItem(manager.full_name, manager.id)
        common.addRow("КМ", self.manager)

        self.comment = QPlainTextEdit(self)
        self.comment.setPlaceholderText("Что именно не так — одной-двумя фразами")
        self.comment.setFixedHeight(70)
        common.addRow("Комментарий", self.comment)
        root.addLayout(common)

        # Две части формы выровнены по одной колонке подписей: иначе поля КМ и
        # комментария начинаются левее остальных.
        labels = [item.widget() for layout in (form, common)
                  for row in range(layout.rowCount())
                  if (item := layout.itemAt(row, QFormLayout.ItemRole.LabelRole))
                  and item.widget() is not None]
        if labels:
            width = max(label.sizeHint().width() for label in labels)
            for label in labels:
                label.setMinimumWidth(width)

        self.info = Hint("", self)
        self.info.hide()
        root.addWidget(self.info)

        root.addWidget(SectionTitle("ТОРГ-2 и фотографии"))
        self.files = QListWidget(self)
        self.files.setMinimumHeight(90)
        self.files.itemDoubleClicked.connect(self._open_file)
        root.addWidget(self.files)

        row = QHBoxLayout()
        self.add_button = QPushButton("Добавить файлы…", self)
        self.add_button.clicked.connect(self._add_files)
        row.addWidget(self.add_button)
        self.remove_button = QPushButton("Убрать", self)
        self.remove_button.clicked.connect(self._remove_file)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        root.addLayout(row)

        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {Palette.DANGER};")
        self.error.hide()
        root.addWidget(self.error)

        buttons = _buttons(self, "Завести" if self.claim is None else "Сохранить",
                           ok_enabled=not self._readonly)
        buttons.accepted.connect(self._accept)
        root.addWidget(buttons)

        if self._readonly:
            for field in (self.kind, self.barcode, self.brand, self.product,
                          self.spec, self.quantity, self.expiry, self.manager):
                field.setEnabled(False)
            self.comment.setReadOnly(True)
            self.add_button.setEnabled(False)
            self.remove_button.setEnabled(False)

    def _build_positions(self, root: QVBoxLayout) -> None:
        """Список позиций поставки — только у новой заявки.

        В одной поставке бракованных позиций бывает несколько. Ввёл позицию,
        нажал «Добавить позицию» — она встала в список, поля очистились для
        следующей. Общие КМ, комментарий и файлы вводятся один раз.
        """
        row = QHBoxLayout()
        self.add_position_button = QPushButton("Добавить позицию", self)
        self.add_position_button.setToolTip(
            "Внести эту позицию в список и ввести следующую из той же поставки")
        self.add_position_button.clicked.connect(self._add_position)
        row.addWidget(self.add_position_button)
        self.remove_position_button = QPushButton("Убрать позицию", self)
        self.remove_position_button.clicked.connect(self._remove_position)
        row.addWidget(self.remove_position_button)
        row.addStretch(1)
        root.addLayout(row)

        self.positions = QListWidget(self)
        self.positions.setMaximumHeight(96)
        self.positions.setVisible(False)
        root.addWidget(self.positions)
        self.positions_hint = Hint(
            "В одной поставке позиций может быть несколько: добавьте каждую. "
            "Для каждой заведётся своя заявка; КМ, комментарий и файлы — общие.", self)
        root.addWidget(self.positions_hint)

    def _fill(self, claim: Claim) -> None:
        self.kind.setCurrentIndex(max(0, self.kind.findData(claim.kind)))
        self.barcode.setText(claim.barcode)
        self.brand.setText(claim.brand)
        self.product.setText(claim.product)
        self.spec.setText(claim.spec)
        self.quantity.setValue(claim.quantity)
        self.expiry.set_value(claim.expiry_date)
        index = self.manager.findData(claim.manager_id)
        if index < 0 and claim.manager_id:
            # КМ, у которого направление сняли: в списке его уже нет, но
            # заявка по-прежнему на нём, и показывать «не назначен» было бы
            # неправдой.
            self.manager.addItem(claim.manager, claim.manager_id)
            index = self.manager.count() - 1
        self.manager.setCurrentIndex(max(0, index))
        self.comment.setPlainText(claim.comment)

        lines = [f"Заявил: {claim.applicant}"
                 + (f" · {claim.created_at:%d.%m.%Y %H:%M}" if claim.created_at else "")]
        if claim.is_closed:
            lines.append(f"Закрыл: {claim.closed_by}"
                         + (f" · {claim.closed_at:%d.%m.%Y %H:%M}" if claim.closed_at else "")
                         + f" · {claim.resolution_title}")
            if claim.close_note:
                lines.append(f"Примечание: {claim.close_note}")
        elif not claim.can_edit:
            lines.append("Править может только заявитель, КМ или администратор.")
        self.info.setText("\n".join(lines))
        self.info.show()

    # --- подсказки ------------------------------------------------------------

    def _lookup(self) -> None:
        """Штрих-код уже заводили — подставляет бренд, название и КМ."""
        if self.claim is not None or not (code := self.barcode.text().strip()):
            return
        run_task(remote.hint, code, self.brand.text().strip(),
                 on_result=self._apply_hint, on_error=lambda _message: None)

    def _brand_typed(self, text: str) -> None:
        """Предупреждает, что такого бренда нет в списке, — пока человек печатает."""
        unknown = bool(self._brands and text.strip() and not has_brand(self._brands, text))
        self.brand_note.setVisible(unknown)
        if unknown:
            self.brand_note.setText("Такого бренда нет в списке — при сохранении предложим его добавить.")

    def _brand_done(self) -> None:
        """Бренд набран до конца: пишем его так, как он в списке, и подбираем КМ."""
        key = brand_key(self.brand.text())
        for brand in self._brands:
            if brand_key(brand.name) == key and brand.name != self.brand.text():
                self.brand.setText(brand.name)
                break
        self._suggest_manager()

    def _suggest_manager(self) -> None:
        if self.claim is not None or self.manager.currentData() is not None:
            return
        if not (brand := self.brand.text().strip()):
            return
        run_task(remote.hint, "", brand, on_result=self._apply_hint,
                 on_error=lambda _message: None)

    def _apply_hint(self, hint: ClaimHint) -> None:
        # Заполняется только пустое: то, что человек уже набрал, важнее памяти
        # базы, и затирать его подсказкой нельзя.
        for field, value in ((self.brand, hint.brand), (self.product, hint.product),
                             (self.spec, hint.spec)):
            if value and not field.text().strip():
                field.setText(value)
        if hint.manager_id is not None and self.manager.currentData() is None:
            if (index := self.manager.findData(hint.manager_id)) >= 0:
                self.manager.setCurrentIndex(index)

    # --- файлы ----------------------------------------------------------------

    def _reload_files(self) -> None:
        if self.claim is None:
            return
        try:
            attachments = remote.list_files(self.claim.id)
        except ServerError as failure:
            self._fail(str(failure))
            return
        self.files.clear()
        for attachment in attachments:
            item = QListWidgetItem(f"{attachment.name}  ·  {attachment.size // 1024 + 1} КБ")
            item.setData(Qt.ItemDataRole.UserRole, attachment)
            self.files.addItem(item)

    def _add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "ТОРГ-2 и фотографии", "", "Все файлы (*.*)")
        for path in paths:
            if self.claim is None:
                self.staged.append(path)
                self.files.addItem(QListWidgetItem(os.path.basename(path)))
                continue
            try:
                remote.attach_file(self.claim.id, path)
            except (OSError, ValueError, ServerError) as failure:
                # Отказ сервера (тип, размер, закрытая заявка) должен быть
                # виден: иначе файл просто не появляется в списке.
                self._fail(f"«{os.path.basename(path)}»: {failure}")
        self._reload_files()

    def _remove_file(self) -> None:
        item = self.files.currentItem()
        if item is None:
            return
        if self.claim is None:
            self.staged.pop(self.files.row(item))
            self.files.takeItem(self.files.row(item))
            return
        try:
            remote.detach_file(item.data(Qt.ItemDataRole.UserRole).id)
        except ServerError as failure:
            self._fail(str(failure))
        self._reload_files()

    def _open_file(self, item: QListWidgetItem) -> None:
        attachment = item.data(Qt.ItemDataRole.UserRole)
        if attachment is None:
            return
        if is_risky(attachment.name) and QMessageBox.warning(
                self, "Запускаемый файл",
                f"«{attachment.name}» — программа или сценарий, а не документ. "
                "Открытие запустит его на этом компьютере.\n\n"
                "Откройте, только если уверены, кто и зачем его приложил.",
                QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Open:
            return
        try:
            os.startfile(remote.local_copy(attachment))  # noqa: S606
        except (OSError, ValueError, ServerError) as failure:
            self._fail(f"Не удалось открыть вложение: {failure}")

    # --- результат ------------------------------------------------------------

    def _fail(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()

    # --- позиции новой заявки ---------------------------------------------------

    @property
    def add_brand(self) -> bool:
        """Попросил ли человек добавить набранный бренд в справочник."""
        return bool(self.new_brands)

    def _has_position(self) -> bool:
        return bool(self.brand.text().strip() or self.product.text().strip()
                    or self.barcode.text().strip())

    def _has_details(self) -> bool:
        """Набрано ли в полях что-то, кроме оставшегося от прошлой позиции бренда.

        Бренд после «Добавить позицию» остаётся в поле, и без этой проверки
        нажатие «Завести» превращало бы его в ещё одну пустую позицию.
        """
        return bool(self.product.text().strip() or self.barcode.text().strip()
                    or self.spec.text().strip() or self.expiry.text().strip())

    def _position_fields(self) -> dict[str, Any]:
        """То, что относится к одной позиции."""
        return {
            "kind": self.kind.currentData(),
            "barcode": self.barcode.text().strip(),
            "brand": self.brand.text().strip(),
            "product": self.product.text().strip(),
            "spec": self.spec.text().strip(),
            "quantity": float(self.quantity.value()),
            "expiry_date": self.expiry.value(),
        }

    @staticmethod
    def _describe_position(position: dict[str, Any]) -> str:
        name = " · ".join(part for part in (position["brand"], position["product"],
                                            position["spec"]) if part)
        quantity = position["quantity"]
        count = str(int(quantity)) if float(quantity).is_integer() else f"{quantity:g}"
        return f"{KIND_TITLES.get(position['kind'], '')}: {name or position['barcode']} — {count} шт"

    def _push_position(self) -> bool:
        """Вносит позицию из полей в список и очищает поля. Ложь — позиция не
        годится, причина показана."""
        if not self._has_position():
            self._fail("Укажите хотя бы бренд, название или штрих-код")
            return False
        if self.expiry.value() is None and self.expiry.text().strip():
            self._fail("Срок годности: дата в виде дд.мм.гггг или пустое поле")
            return False
        position = self._position_fields()
        self._positions.append(position)
        self.positions.addItem(self._describe_position(position))
        self.positions.setVisible(True)
        self.error.hide()
        self._clear_position()
        return True

    def _clear_position(self) -> None:
        """Поля очищаются для следующей позиции. Вид и бренд остаются: в одной
        поставке позиции чаще всего одного рода и одного бренда, а другой бренд
        печатается поверх."""
        for field in (self.barcode, self.product, self.spec, self.expiry):
            field.clear()
        self.quantity.setValue(1)
        self.barcode.setFocus()

    def _add_position(self) -> None:
        self._push_position()

    def _remove_position(self) -> None:
        row = self.positions.currentRow()
        if row < 0:
            return
        self._positions.pop(row)
        self.positions.takeItem(row)
        self.positions.setVisible(bool(self._positions))

    def _common_fields(self) -> dict[str, Any]:
        """Общее для поставки. КМ передаётся всегда: пустое значение здесь —
        «снять», а не «не менять»."""
        return {"comment": self.comment.toPlainText().strip(),
                "manager_id": self.manager.currentData()}

    def fields(self) -> dict[str, Any]:
        """Поля формы для сервера: позиция в полях и общие данные."""
        return {**self._position_fields(), **self._common_fields()}

    def claims_fields(self) -> list[dict[str, Any]]:
        """Поля каждой заявки новой поставки: своя позиция и общие КМ с
        комментарием."""
        common = self._common_fields()
        return [{**position, **common} for position in self._positions]

    def changes(self) -> dict[str, Any]:
        """Только то, что поменялось, — для правки. Пустой словарь — менять нечего."""
        claim = self.claim
        assert claim is not None
        before = {
            "kind": claim.kind, "barcode": claim.barcode, "brand": claim.brand,
            "product": claim.product, "spec": claim.spec, "quantity": claim.quantity,
            "expiry_date": claim.expiry_date, "comment": claim.comment,
            "manager_id": claim.manager_id,
        }
        return {name: value for name, value in self.fields().items()
                if before[name] != value}

    def _confirm_new_brands(self, brands: list[str]) -> bool:
        """Брендов нет в списке: спрашивает один раз, добавить ли. Ложь —
        человек передумал.

        Пустой справочник (сервер без него) не спрашивает: добавлять некуда.
        У заведённой заявки спрашивается, только если бренд поменяли.
        """
        unknown: list[str] = []
        for brand in brands:
            if (not brand or not self._brands or has_brand(self._brands, brand)
                    or any(brand_key(brand) == brand_key(u) for u in unknown)):
                continue
            if self.claim is not None and brand_key(self.claim.brand) == brand_key(brand):
                continue
            unknown.append(brand)
        if not unknown:
            return True
        if len(unknown) == 1:
            text = f"Бренда «{unknown[0]}» нет в списке."
        else:
            text = "Этих брендов нет в списке: " + ", ".join(f"«{b}»" for b in unknown) + "."
        answer = QMessageBox.question(
            self, "Новый бренд",
            text + "\n\nДобавить, чтобы подсказывались в следующих заявках?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes)
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        self.new_brands = unknown if answer == QMessageBox.StandardButton.Yes else []
        return True

    def _accept(self) -> None:
        if self.claim is None:
            # Позиция, которую успели набрать, но не добавили, не теряется: она
            # входит в поставку последней.
            if self._has_details() or (not self._positions and self._has_position()):
                if not self._push_position():
                    return
            if not self._positions:
                self._fail("Укажите хотя бы бренд, название или штрих-код")
                return
            brands = [position["brand"] for position in self._positions]
        else:
            data = self.fields()
            if not (data["brand"] or data["product"] or data["barcode"]):
                self._fail("Укажите хотя бы бренд, название или штрих-код")
                return
            if data["expiry_date"] is None and self.expiry.text().strip():
                self._fail("Срок годности: дата в виде дд.мм.гггг или пустое поле")
                return
            brands = [data["brand"]]
        if not self._confirm_new_brands(brands):
            return
        self.accept()


class CloseDialog(QDialog):
    """Чем кончилось: без итога заявку не закрыть.

    Итог обязателен, потому что именно он отличает архив от свалки: по нему
    потом видно, сколько вернули деньгами, а сколько просто довезли.
    """

    def __init__(self, claim: Claim, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Закрыть заявку")
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle(claim.title))
        root.addWidget(Hint(
            f"{claim.kind_title}, {claim.quantity_text()} шт. После закрытия заявка "
            "уйдёт в архив и правке не подлежит."))

        form = QFormLayout()
        form.setSpacing(Metrics.GAP)
        self.resolution = SelectBox(self)
        self.resolution.addItem("— выберите итог —", "")
        for code, title in RESOLUTION_TITLES.items():
            self.resolution.addItem(title, code)
        form.addRow("Итог", self.resolution)

        self.note = QLineEdit(self)
        self.note.setPlaceholderText("дата довоза — по желанию")
        form.addRow("Примечание", self.note)
        root.addLayout(form)

        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {Palette.DANGER};")
        self.error.hide()
        root.addWidget(self.error)

        buttons = _buttons(self, "Закрыть заявку")
        buttons.accepted.connect(self._accept)
        root.addWidget(buttons)

    def _accept(self) -> None:
        if not self.resolution.currentData():
            self.error.setText("Выберите, чем закончилась заявка")
            self.error.show()
            return
        self.accept()

    def value(self) -> tuple[str, str]:
        return str(self.resolution.currentData()), self.note.text().strip()
