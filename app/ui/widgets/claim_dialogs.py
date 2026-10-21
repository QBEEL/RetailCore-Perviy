"""Окна Торг-2: заявка, закрытие."""
from __future__ import annotations

import os
from typing import Any

from PySide6.QtCore import QStringListModel, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
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
    KINDS_WITH_ITEM,
    RESOLUTION_TITLES,
    Brand,
    Claim,
    ClaimHint,
    Manager,
    brand_key,
    has_brand,
    remote,
)
from ...core.claims import mark as mark_module
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


def _ask_gis(code: str) -> Any:
    """Спрашивает «Честный ЗНАК» о коде. Свежий ответ: срок читается из самого
    ответа, а в кэше проверки его нет."""
    from ...core.marking import service

    return service.check([code], use_cache=False)[0]


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
                 parent: QWidget | None = None, *, settings: Any = None) -> None:
        super().__init__(parent)
        self.claim = claim
        self._managers = managers
        self._brands = brands or []
        # Правила расчёта срока по дате производства (вкладка «Срок годности»
        # в маркировке): у парфюмерии срока в системе нет, он считается.
        self._shelf_years = getattr(settings, "marking_shelf_years", 3) or 3
        self._shelf_brands = dict(getattr(settings, "marking_shelf_brands", {}) or {})
        # Какой срок подставлен из кода: свой, введённый руками, не затирается.
        self._auto_expiry = ""
        # Последний обработанный код: Enter от сканера и уход фокуса из поля
        # приходят оба, а спрашивать «Честный ЗНАК» дважды незачем.
        self._scanned = ""
        # Номер ответа ГИС МТ: медленный ответ по прежнему коду не должен лечь
        # поверх следующего.
        self._lookup_id = 0
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

        # Маркируемый товар: у брака и излишка код с упаковки обязателен, у
        # недопоставки его можно только приложить — товара на складе нет.
        self.marked = QCheckBox("Маркируемый товар", self)
        self.marked.toggled.connect(self._marked_toggled)
        self.kind.currentIndexChanged.connect(lambda _i: self._refresh_mark_hint())
        form.addRow("", self.marked)
        self.mark_code = QLineEdit(self)
        self.mark_code.setPlaceholderText("наведите сканер на код маркировки (DataMatrix)")
        self.mark_code.returnPressed.connect(self._code_scanned)
        self.mark_code.editingFinished.connect(self._code_scanned)
        form.addRow("Код маркировки", self.mark_code)
        self.mark_note = Hint("", self)
        form.addRow("", self.mark_note)
        self._mark_rows = (self.mark_code, self.mark_note)
        for widget in self._mark_rows:
            form.setRowVisible(widget, False)
        self._form = form

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
        # Сканер заканчивает каждый код нажатием Enter. Для окна это «кнопка по
        # умолчанию»: заявка закрывалась на середине ввода. Теперь ни одна кнопка
        # от Enter не срабатывает (см. keyPressEvent) — нажимают их мышью.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

        if self._readonly:
            for field in (self.kind, self.barcode, self.marked, self.mark_code,
                          self.brand, self.product, self.spec, self.quantity,
                          self.expiry, self.manager):
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
        self.mark_code.setText(claim.mark_code)
        self.marked.setChecked(bool(claim.mark_code))
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
        # Этот штрих-код уже заводили с кодом маркировки — товар маркируемый.
        # Галочку только ставим, снимать за человека нечего.
        if getattr(hint, "marked", False) and not self.marked.isChecked():
            self.marked.setChecked(True)

    # --- маркировка -----------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 — имя Qt
        """Enter не закрывает окно и не нажимает кнопки.

        Сканер завершает скан нажатием Enter, и обычное поведение окна — нажать
        кнопку по умолчанию — завершало заявку посреди ввода. Enter остаётся
        рабочим там, где он нужен: в многострочном комментарии и на кнопке,
        на которой стоит фокус.
        """
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            focus = self.focusWidget()
            if isinstance(focus, (QPushButton, QPlainTextEdit)):
                super().keyPressEvent(event)
            else:
                event.accept()
            return
        super().keyPressEvent(event)

    def _code_value(self) -> str:
        """Код для сохранения: пусто, если товар не отмечен маркируемым."""
        if not self.marked.isChecked():
            return ""
        return mark_module.read(self.mark_code.text()).code

    def _kind(self) -> str:
        return str(self.kind.currentData() or "")

    def _marked_toggled(self, checked: bool) -> None:
        for widget in self._mark_rows:
            self._form.setRowVisible(widget, checked)
        if checked and self.isVisible() and not self._readonly:
            self.mark_code.setFocus()
        self._refresh_mark_hint()
        self.adjustSize()

    def _refresh_mark_hint(self) -> None:
        """Что нужно от человека: код обязателен только у брака и излишка."""
        if not self.marked.isChecked() or self.mark_code.text().strip():
            return
        if mark_module.needs_code(self._kind(), True):
            self.mark_note.setText(
                "Код обязателен: отсканируйте его с упаковки. Из кода подставятся "
                "штрих-код и срок годности.")
        else:
            self.mark_note.setText(
                "У недопоставки товара нет на складе — код можно не сканировать.")

    def _mark_problem(self) -> str:
        """Почему маркировку сохранить нельзя; пусто — всё в порядке."""
        if not self.marked.isChecked():
            return ""
        read = mark_module.read(self.mark_code.text())
        if not read.code:
            if mark_module.needs_code(self._kind(), True):
                return ("Товар маркируемый — отсканируйте код маркировки. Если товар "
                        "не маркируемый, снимите галочку")
            return ""
        if not read.valid:
            return f"Код маркировки не разобран: {read.problem}"
        return ""

    def _code_scanned(self) -> None:
        """Код отсканирован (или введён): штрих-код и срок годности — из него."""
        text = self.mark_code.text().strip()
        if not text or self._readonly:
            return
        read = mark_module.read(text, self._shelf_years, self._shelf_brands)
        if not read.valid:
            self.mark_note.setText(f"Не похоже на код маркировки: {read.problem}")
            return
        if read.code == self._scanned:
            return
        self._scanned = read.code
        # Сканер мог набрать код русскими буквами или с хвостом пробелов:
        # в поле остаётся нормальный вид, как он и будет сохранён.
        if self.mark_code.text() != read.code:
            self.mark_code.setText(read.code)
        if not self.barcode.text().strip() and read.barcode:
            self.barcode.setText(read.barcode)
            self._lookup()
        self.error.hide()
        self._apply_read(read)
        if read.needs_lookup:
            self._lookup_expiry(read)

    def _apply_read(self, read: mark_module.MarkRead) -> None:
        """Переносит прочитанное в поля. Своё, набранное руками, не затирается."""
        for field, value in ((self.brand, read.brand), (self.product, read.name)):
            if value and not field.text().strip():
                field.setText(value)
        if read.expires is None:
            self.mark_note.setText(
                "Срока годности в коде нет." if not read.needs_lookup
                else "Срока в самом коде нет — спрашиваю «Честный ЗНАК»…")
            return
        shown = self.expiry.text().strip()
        if not shown or shown == self._auto_expiry:
            self.expiry.set_value(read.expires)
            self._auto_expiry = self.expiry.text()
        source = (f"рассчитан от даты производства (+{read.shelf_years} л.)"
                  if read.estimated else "взят из кода")
        self.mark_note.setText(f"Срок годности {read.expires:%d.%m.%Y} {source}.")

    def _lookup_expiry(self, read: mark_module.MarkRead) -> None:
        """Срока в самом коде нет — спрашиваем «Честный ЗНАК», если есть вход."""
        from ...core.marking import service  # тяжёлый импорт — только когда нужен

        if not service.signed_in():
            self.mark_note.setText(
                "Срока в коде нет. Чтобы он подставлялся сам, войдите по сертификату "
                "на вкладке «Проверка кодов»; пока укажите срок вручную.")
            return
        self._lookup_id += 1
        lookup_id = self._lookup_id
        run_task(_ask_gis, read.code,
                 on_result=lambda info: self._gis_answered(lookup_id, read, info),
                 on_error=lambda message: self._gis_failed(lookup_id, message))

    def _gis_answered(self, lookup_id: int, read: mark_module.MarkRead, info: Any) -> None:
        if lookup_id != self._lookup_id or self.mark_code.text() != read.code:
            return
        done = mark_module.complete(read, info, self._shelf_years, self._shelf_brands)
        self._apply_read(done)
        if done.expires is None:
            self.mark_note.setText(
                "«Честный ЗНАК» не назвал срок годности по этому коду — укажите вручную.")

    def _gis_failed(self, lookup_id: int, message: str) -> None:
        if lookup_id == self._lookup_id:
            self.mark_note.setText(
                f"Срок спросить не удалось ({message.strip()}) — укажите его вручную.")

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
                    or self.spec.text().strip() or self.expiry.text().strip()
                    or self.mark_code.text().strip())

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
            "mark_code": self._code_value(),
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
        if problem := self._mark_problem():
            self._fail(problem)
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
        for field in (self.barcode, self.product, self.spec, self.expiry, self.mark_code):
            field.clear()
        # Следующая позиция может быть немаркируемой: галочку она поставит сама,
        # когда отсканируют код или штрих-код, по которому он уже был.
        self.marked.setChecked(False)
        self._auto_expiry = ""
        self._scanned = ""
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
            "expiry_date": claim.expiry_date, "mark_code": claim.mark_code,
            "comment": claim.comment, "manager_id": claim.manager_id,
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
            if problem := self._mark_problem():
                self._fail(problem)
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

    def __init__(self, claim: Claim, parent: QWidget | None = None,
                 *, count: int = 1) -> None:
        super().__init__(parent)
        self.setWindowTitle("Закрыть заявку" if count == 1 else "Закрыть заявки")
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        if count == 1:
            root.addWidget(SectionTitle(claim.title))
            root.addWidget(Hint(
                f"{claim.kind_title}, {claim.quantity_text()} шт. После закрытия заявка "
                "уйдёт в архив и правке не подлежит."))
        else:
            # Один итог и одно примечание на все выделенные: заявки одной поставки
            # обычно кончаются одинаково, а разные закрывают по отдельности.
            root.addWidget(SectionTitle(f"Закрыть заявок: {count}"))
            root.addWidget(Hint(
                "Выбранный итог и примечание получат все выделенные заявки. После "
                "закрытия они уйдут в архив и правке не подлежат. Если у заявок "
                "разные итоги, закройте их по отдельности."))

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

        buttons = _buttons(self, "Закрыть заявку" if count == 1 else f"Закрыть заявки ({count})")
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
