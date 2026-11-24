"""Вкладка создания карточек Национального каталога."""
from __future__ import annotations

import csv
from collections.abc import Callable
from io import StringIO
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from ...core.marking import catalog, transport
from ...core.marking.models import Contour
from ..tasks import run_task


class TnvedDialog(QDialog):
    def __init__(self, codes: list[tuple[str, str]], parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Коды ТН ВЭД")
        self._codes = codes
        layout = QVBoxLayout(self)
        columns = QHBoxLayout()
        self.codes = QListWidget(self)
        self.codes.setAccessibleName("Коды ТН ВЭД")
        self.codes.addItems([code for code, _description in codes])
        self.codes.setMaximumWidth(170)
        columns.addWidget(self.codes)
        self.description = QPlainTextEdit(self)
        self.description.setAccessibleName("Описание выбранного кода ТН ВЭД")
        self.description.setReadOnly(True)
        columns.addWidget(self.description, 1)
        layout.addLayout(columns, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Выбрать")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.codes.currentRowChanged.connect(self._show_description)
        self.codes.itemDoubleClicked.connect(self.accept)
        self.codes.setCurrentRow(0)
        self.codes.setFocus()

        available = parent.window().screen().availableGeometry()
        self.setMaximumSize(max(1, available.width() - 32), max(1, available.height() - 64))
        self.resize(680, 400)
        self.move(available.center() - self.rect().center())

    def _show_description(self, row: int) -> None:
        self.description.setPlainText(self._codes[row][1] if row >= 0 else "")

    def selected_code(self) -> str:
        row = self.codes.currentRow()
        return self._codes[row][0] if row >= 0 else ""


class MissingAttributesDialog(QDialog):
    def __init__(self, fields: list[dict], suggestions: dict[str, tuple[str, str]],
                 parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Недостающие атрибуты")
        layout = QVBoxLayout(self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        content = QWidget(scroll)
        form = QFormLayout(content)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self._inputs = {}
        for field in fields:
            attr_id = catalog.positive_id(field.get("attr_id"))
            if not attr_id:
                continue
            title = QLabel(f"{field.get('attr_name') or 'Атрибут'} ({attr_id})", content)
            title.setTextFormat(Qt.TextFormat.PlainText)
            title.setWordWrap(True)
            row = QWidget(content)
            columns = QHBoxLayout(row)
            columns.setContentsMargins(0, 0, 0, 0)
            value, value_type = suggestions.get(attr_id, ("", ""))
            value_input = self._input(field.get("attr_preset"), value,
                                      editable=field.get("attr_preset_only") is not True)
            columns.addWidget(value_input, 1)
            type_input = None
            if field.get("attr_value_type") or value_type:
                type_input = self._input(field.get("attr_value_type"), value_type, editable=False)
                type_input.setMinimumWidth(160)
                type_input.setMaximumWidth(220)
                type_input.setAccessibleName("Единица измерения или тип значения")
                if isinstance(type_input, QComboBox):
                    type_input.setItemText(0, "Ед. изм.")
                    type_input.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
                else:
                    type_input.setPlaceholderText("Ед. изм.")
                columns.addWidget(type_input)
            form.addRow(title, row)
            self._inputs[attr_id] = value_input, type_input

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Добавить в форму")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(660, min(450, 100 + 45 * len(fields)))

    @staticmethod
    def _input(items: object, value: str, *, editable: bool) -> QLineEdit | QComboBox:
        choices = ([item for item in items if isinstance(item, str) and item.strip()]
                   if isinstance(items, list) else [])
        if not choices:
            return QLineEdit(value)
        combo = QComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.addItem("")
        combo.addItems(choices)
        combo.setEditable(editable)
        if value and editable:
            combo.setEditText(value)
        elif value and (index := combo.findText(value, Qt.MatchFlag.MatchFixedString)) >= 0:
            combo.setCurrentIndex(index)
        return combo

    @staticmethod
    def _value(widget: QLineEdit | QComboBox | None) -> str:
        if isinstance(widget, QComboBox):
            return widget.currentText().strip() if widget.isEditable() or widget.currentIndex() > 0 else ""
        return widget.text().strip() if widget is not None else ""

    def attributes(self) -> list[tuple[str, str, str]]:
        return [(attr_id, value, self._value(value_type))
                for attr_id, (field, value_type) in self._inputs.items()
                if (value := self._value(field))]


class MarkingCatalogTab(QScrollArea):
    def __init__(self, contour: Callable[[], Contour], parent: QWidget) -> None:
        super().__init__(parent)
        self._contour = contour
        self._last_submission: tuple[catalog.CatalogAccount, str] | None = None
        self._source_card: catalog.CardDraft | None = None
        self._missing_fields: tuple[catalog.CatalogAccount, tuple[str, ...]] | None = None
        self._generated_gtins: dict[str, tuple[Contour, str]] = {}
        self.setWidgetResizable(True)
        content = QWidget(self)
        layout = QVBoxLayout(content)
        self.setWidget(content)

        self.form_panel = QWidget(content)
        form = QFormLayout(self.form_panel)
        self.kind = QComboBox()
        for kind in catalog.CardKind:
            self.kind.addItem(kind.value, kind)
        self.name = QLineEdit()
        self.brand = QLineEdit()
        self.tnved = QLineEdit()
        self.tnved.setPlaceholderText("Код или первые четыре цифры для поиска")
        self.gtin = QLineEdit()
        self.category = QLineEdit()
        self.category.setPlaceholderText("Необязательно, если ТН ВЭД однозначен")
        self.set_group = QComboBox()
        self.set_group.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.set_group.addItem("Выберите товарную группу", "")
        for group_id, (_, title) in catalog.SET_GROUPS.items():
            self.set_group.addItem(title, group_id)
        self.components = QPlainTextEdit()
        self.components.setPlaceholderText("GTIN;количество — по одному вложению на строку")
        self.components.setMaximumHeight(86)
        self.attributes = QPlainTextEdit()
        self.attributes.setPlaceholderText("ID;значение;тип значения — по одному атрибуту на строку")
        self.attributes.setToolTip("Тип значения необязателен. Для единиц измерения укажите "
                                   "тип из справочника атрибутов.")
        self.attributes.setMaximumHeight(86)
        self.moderation = QCheckBox("Сразу отправить на модерацию")
        form.addRow("Тип карточки", self.kind)
        form.addRow("Полное наименование", self.name)
        form.addRow("Товарный знак", self.brand)
        tnved_button = QPushButton("Подобрать ТН ВЭД")
        tnved_button.clicked.connect(self._find_tnved)
        form.addRow("Код ТН ВЭД", self._field_with_button(self.tnved, tnved_button))
        generate_button = QPushButton("Сгенерировать GTIN")
        generate_button.clicked.connect(self._generate_gtin)
        self.gtin_row = self._field_with_button(self.gtin, generate_button)
        form.addRow("GTIN", self.gtin_row)
        self.technical_gtin = QLabel("Каталог присвоит GTIN с префиксом 029 после создания.")
        self.technical_gtin.setWordWrap(True)
        form.addRow("GTIN", self.technical_gtin)
        category_button = QPushButton("Найти по ТН ВЭД")
        category_button.clicked.connect(self._find_categories)
        form.addRow("ID категории", self._field_with_button(self.category, category_button))
        form.addRow("Товарная группа набора", self.set_group)
        form.addRow("Состав набора", self.components)
        attributes_button = QPushButton("Показать атрибуты категории")
        attributes_button.clicked.connect(self._show_attributes)
        form.addRow("Атрибуты", self._field_with_button(self.attributes, attributes_button))
        form.addRow(self.moderation)
        self.create_button = QPushButton("Создать карточку")
        self.create_button.clicked.connect(self._create)
        form.addRow(self.create_button)
        layout.addWidget(self.form_panel)

        self.status_panel = QWidget(content)
        status_form = QFormLayout(self.status_panel)
        self.feed_id = QLineEdit()
        self.feed_id.setPlaceholderText("Номер, полученный при создании карточки")
        self.status_button = QPushButton("Проверить отправку")
        self.status_button.clicked.connect(self._check_status)
        status_form.addRow("Номер отправки", self._field_with_button(self.feed_id, self.status_button))
        self.fill_missing_button = QPushButton("Заполнить недостающие атрибуты…")
        self.fill_missing_button.clicked.connect(self._fill_missing_attributes)
        status_form.addRow(self.fill_missing_button)
        self.feed_id.textChanged.connect(self._reset_missing_attributes)
        self._reset_missing_attributes()
        layout.addWidget(self.status_panel)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setPlaceholderText("Здесь появятся результат обработки и ошибки Национального каталога.")
        self.result.setMinimumHeight(82)
        layout.addWidget(self.result, 1)
        self.kind.currentIndexChanged.connect(self._sync_kind)
        self.tnved.textChanged.connect(self.category.clear)
        self._sync_kind()

    @staticmethod
    def _field_with_button(field: QWidget, button: QPushButton) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(field, 1)
        layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignTop)
        return row

    def _sync_kind(self) -> None:
        kind = self.kind.currentData()
        form = self.form_panel.layout()
        form.setRowVisible(self.gtin_row, kind is not catalog.CardKind.TECHNICAL)
        form.setRowVisible(self.technical_gtin, kind is catalog.CardKind.TECHNICAL)
        form.setRowVisible(self.set_group, kind is catalog.CardKind.SET)
        form.setRowVisible(self.components, kind is catalog.CardKind.SET)

    @staticmethod
    def _rows(field: QPlainTextEdit, separator_count: int) -> list[list[str]]:
        return [line.split(";", separator_count) for line in field.toPlainText().splitlines()
                if line.strip()]

    @property
    def busy(self) -> bool:
        return not self.form_panel.isEnabled()

    def load_technical_card(self, draft: catalog.CardDraft) -> bool:
        if self.busy:
            return False
        fields = (self.name, self.brand, self.tnved, self.gtin, self.category)
        if (any(field.text().strip() for field in fields)
                or self.components.toPlainText().strip() or self.attributes.toPlainText().strip()):
            if QMessageBox.question(
                    self, "Заполнение технической карточки",
                    "Заменить текущие данные формы сведениями из проверки кода?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
            ) != QMessageBox.StandardButton.Yes:
                return False
        self.kind.setCurrentIndex(self.kind.findData(catalog.CardKind.TECHNICAL))
        self.name.setText(draft.name)
        self.brand.setText(draft.brand)
        self.tnved.setText(draft.tnved)
        self.gtin.clear()
        self.category.clear()
        self.set_group.setCurrentIndex(0)
        self.components.clear()
        encoded = StringIO()
        csv.writer(encoded, delimiter=";", lineterminator="\r\n").writerows(draft.attributes)
        self.attributes.setPlainText(encoded.getvalue().rstrip("\r\n"))
        self.moderation.setChecked(False)
        self._source_card = draft
        missing = [label for value, label in ((draft.name, "наименование"),
                   (draft.brand, "товарный знак"), (draft.tnved, "код ТН ВЭД")) if not value]
        message = ("Заполните недостающие поля: " + ", ".join(missing) + "."
                   if missing else "Данные перенесены из проверки кода. Проверьте поля перед созданием.")
        self.result.setPlainText(message)
        return True

    def _draft(self) -> catalog.CardDraft:
        kind = self.kind.currentData()
        components = []
        if kind is catalog.CardKind.SET:
            for fields in self._rows(self.components, 1):
                if len(fields) != 2:
                    raise ValueError("Состав набора заполните в формате «GTIN;количество».")
                components.append((fields[0], fields[1]))
        attributes = []
        try:
            for fields in csv.reader(StringIO(self.attributes.toPlainText()), delimiter=";", strict=True):
                if not fields or (len(fields) == 1 and not fields[0].strip()):
                    continue
                if not 2 <= len(fields) <= 3:
                    raise ValueError("Заполните атрибуты в формате «ID;значение;тип значения».")
                attributes.append((fields[0], fields[1], fields[2] if len(fields) == 3 else ""))
        except csv.Error as error:
            raise ValueError("Проверьте кавычки в строках атрибутов.") from error
        name, brand, tnved = (self.name.text(), self.brand.text(), self.tnved.text())
        source = self._source_card
        source_gtin = (source.source_gtin if kind is catalog.CardKind.TECHNICAL and source
                       and (name.strip(), brand.strip(), tnved.strip())
                       == (source.name, source.brand, source.tnved) else "")
        return catalog.CardDraft(
            kind, name, brand, tnved,
            gtin=self.gtin.text(), category=self.category.text(), set_group=self.set_group.currentData(),
            components=tuple(components), attributes=tuple(attributes),
            moderation=self.moderation.isChecked(),
            source_gtin=source_gtin,
        )

    def _account(self) -> catalog.CatalogAccount | None:
        try:
            return catalog.CatalogAccount.current(self._contour())
        except transport.MarkingError as error:
            self.result.setPlainText(str(error))
            return None

    def _create(self) -> None:
        try:
            draft = self._draft()
            draft.payload()
        except ValueError as error:
            self.result.setPlainText(str(error))
            return
        if (account := self._account()) is None:
            return
        owner = self._generated_gtins.get(draft.gtin.strip().zfill(14))
        if (draft.kind is not catalog.CardKind.TECHNICAL and owner
                and owner != (account.contour, account.inn)):
            self.result.setPlainText(
                "Этот GTIN сгенерирован под другим ИНН или в другом контуре. Укажите другой код.")
            return
        action = "Создать и отправить на модерацию" if draft.moderation else "Создать черновик"
        reply = QMessageBox.question(
            self, "Создание карточки",
            f"{action}?\n\n{draft.kind.value} · {draft.name.strip()}\n"
            f"ИНН {account.inn} · {account.contour.title}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._run(account.create, draft, on_result=lambda feed_id: self._created(account, feed_id))

    def _generate_gtin(self) -> None:
        if self.kind.currentData() is catalog.CardKind.TECHNICAL:
            return
        if (account := self._account()) is None:
            return
        if self.gtin.text().strip() and QMessageBox.question(
                self, "Генерация GTIN",
                f"Сгенерировать новый GTIN и подставить его в поле?\n"
                f"ИНН {account.inn} · {account.contour.title}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run(account.generate_gtin, on_result=lambda gtin: self._set_generated_gtin(account, gtin))

    def _set_generated_gtin(self, account: catalog.CatalogAccount, gtin: str) -> None:
        self._generated_gtins[gtin.zfill(14)] = account.contour, account.inn
        self.gtin.setText(gtin)
        self.result.setPlainText(f"GTIN {gtin} сгенерирован для ИНН {account.inn} · {account.contour.title}.")

    def _created(self, account: catalog.CatalogAccount, feed_id: str) -> None:
        self._last_submission = account, feed_id
        self.feed_id.setText(feed_id)
        self.result.setPlainText(
            f"Каталог принял отправку № {feed_id}.\n"
            f"ИНН {account.inn} · {account.contour.title}\n"
            "Нажмите «Проверить отправку», чтобы узнать результат обработки."
        )

    def _check_status(self) -> None:
        if not (feed_id := catalog.positive_id(self.feed_id.text())):
            self.result.setPlainText("Введите числовой номер отправки.")
            return
        if (account := self._account()) is None:
            return
        if self._last_submission:
            previous_account, previous_id = self._last_submission
            if (feed_id == previous_id and (account.contour, account.inn)
                    != (previous_account.contour, previous_account.inn)):
                self.result.setPlainText(
                    f"Отправка № {feed_id} выполнена в контуре «{previous_account.contour.title}» "
                    f"под ИНН {previous_account.inn}."
                )
                return
        self._reset_missing_attributes()
        self._run(account.feed_status, feed_id,
                  on_result=lambda status: self._show_status(account, feed_id, status))

    def _show_status(self, account: catalog.CatalogAccount, feed_id: str,
                     status: dict[str, Any]) -> None:
        state = status["status"]
        lines = [f"Отправка № {feed_id} · {catalog.FEED_STATUSES.get(state, state)}",
                 f"ИНН {account.inn} · {account.contour.title}"]
        details = status.get("result")
        if isinstance(details, dict):
            for position, messages in details.items():
                if str(position).isdigit() and isinstance(messages, list):
                    lines.extend(message for message in messages if isinstance(message, str))
            if count := catalog.positive_id(details.get("totalErrors")):
                lines.append(f"Ошибок: {count}")
        items = status.get("item")
        items = list(items) if isinstance(items, list) else []
        errors = status.get("error_details")
        if isinstance(errors, dict):
            if isinstance(common := errors.get("commonError"), dict) and common.get("text"):
                lines.append(str(common["text"]))
            if isinstance(nested_items := errors.get("items"), list):
                items.extend(nested_items)
        for item in items:
            if not isinstance(item, dict):
                continue
            fields = (item.get("gtin"), item.get("attribute_name"),
                      item.get("message") or item.get("status_message"))
            if line := " · ".join(str(value) for value in fields if value):
                lines.append(line)
        if gtin := status.get("gtin"):
            lines.append(f"GTIN: {gtin}")
        self._reset_missing_attributes()
        if missing_ids := catalog.missing_attribute_ids(lines[2:]):
            self._missing_fields = account, missing_ids
            self.fill_missing_button.show()
        self.result.setPlainText("\n".join(dict.fromkeys(lines)))

    def _reset_missing_attributes(self) -> None:
        self._missing_fields = None
        self.fill_missing_button.hide()

    def _fill_missing_attributes(self) -> None:
        if not self._missing_fields:
            return
        account, attr_ids = self._missing_fields
        if (current := self._account()) is None:
            return
        if (current.inn, current.contour) != (account.inn, account.contour):
            self.result.setPlainText("Проверьте отправку под ИНН и в контуре, где она была создана.")
            return
        try:
            draft = self._draft()
        except ValueError as error:
            self.result.setPlainText(str(error))
            return
        filled = {catalog.positive_id(attr_id) for attr_id, value, _ in draft.attributes if value.strip()}
        pending = tuple(attr_id for attr_id in attr_ids if attr_id not in filled)
        if not pending:
            self.result.setPlainText("В форме уже есть значения для всех указанных атрибутов.")
            return
        self._run(current.missing_attribute_fields, draft, pending,
                  on_result=self._apply_missing_attributes)

    def _apply_missing_attributes(self, result: tuple[list[dict], dict[str, tuple[str, str]]]) -> None:
        fields, suggestions = result
        dialog = MissingAttributesDialog(fields, suggestions, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not (values := dialog.attributes()):
            self.result.setPlainText("Значения атрибутов в форме не изменились.")
            return
        encoded = StringIO()
        csv.writer(encoded, delimiter=";", lineterminator="\r\n").writerows(values)
        self.attributes.appendPlainText(encoded.getvalue().rstrip("\r\n"))
        self.result.setPlainText(
            f"В форму добавлено атрибутов: {len(values)}. Проверьте значения перед отправкой."
        )

    def _find_categories(self) -> None:
        try:
            tnved = catalog.validate_tnved(self.tnved.text())
        except ValueError as error:
            self.result.setPlainText(str(error))
            return
        if (account := self._account()) is not None:
            self._run(account.categories, tnved, on_result=self._choose_category)

    def _find_tnved(self) -> None:
        group = ""
        if self.kind.currentData() is catalog.CardKind.SET:
            group = catalog.SET_GROUPS.get(self.set_group.currentData(), ("", ""))[0]
        if (account := self._account()) is not None:
            self._run(account.search_tnved, self.tnved.text(), group, on_result=self._choose_tnved)

    def _choose_tnved(self, codes: list[tuple[str, str]]) -> None:
        if not codes:
            self.result.setPlainText("По указанным условиям ГИС МТ не вернула коды ТН ВЭД.")
            return
        dialog = TnvedDialog(codes, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and (code := dialog.selected_code()):
            self.tnved.setText(code)

    def _choose_category(self, categories: list[dict]) -> None:
        choices = {}
        for category in categories:
            if not isinstance(category, dict):
                continue
            category_id = catalog.positive_id(category.get("cat_id"))
            if category.get("category_active") is True and category_id and category.get("cat_name"):
                choices[f"{category['cat_name']} · {category_id}"] = category_id
        if not choices:
            self.result.setPlainText("Для этого кода ТН ВЭД каталог не вернул активных категорий.")
            return
        choice, accepted = QInputDialog.getItem(
            self, "Категория Национального каталога", "Категория", list(choices), editable=False
        )
        if accepted:
            self.category.setText(choices[choice])

    def _show_attributes(self) -> None:
        if (account := self._account()) is not None:
            self._run(account.attributes, self.tnved.text(), self.category.text(),
                      self.kind.currentData(), on_result=self._attribute_reference)

    def _attribute_reference(self, attributes: list[dict]) -> None:
        fields = [item for item in attributes if isinstance(item, dict)
                  and item.get("attr_id") and item.get("attr_name")]
        fields.sort(key=lambda item: (item.get("attr_type") != "m",
                                      item.get("first_layer") is not True))
        lines = []
        for item in fields:
            title = f"{item['attr_id']} · {item['attr_name']}"
            if item.get("attr_type") == "m":
                title += " · обязательный"
            if item.get("first_layer") is True:
                title += " · для заказа кодов маркировки"
            lines.append(title)
            for label, key in (("Значения", "attr_preset"), ("Типы значений", "attr_value_type")):
                values = item.get(key)
                if isinstance(values, list) and values:
                    lines.append(f"  {label}: " + ", ".join(str(value) for value in values))
        if not lines:
            self.result.setPlainText("Каталог не вернул атрибуты для выбранной категории.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Атрибуты Национального каталога")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Используйте ID из списка при заполнении атрибутов карточки."))
        reference = QPlainTextEdit("\n".join(lines), dialog)
        reference.setReadOnly(True)
        layout.addWidget(reference)
        close = QPushButton("Закрыть", dialog)
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.resize(600, 440)
        dialog.exec()

    def _set_busy(self, busy: bool) -> None:
        self.form_panel.setEnabled(not busy)
        self.status_panel.setEnabled(not busy)

    def _run(self, function: Callable, *args: Any, on_result: Callable) -> None:
        self._set_busy(True)
        self.result.setPlainText("Запрос к Национальному каталогу…")

        def finished(value: Any) -> None:
            self._set_busy(False)
            self.result.clear()
            on_result(value)

        run_task(function, *args, on_result=finished, on_error=self._request_failed)

    def _request_failed(self, message: str) -> None:
        self._set_busy(False)
        self.result.setPlainText(message)
