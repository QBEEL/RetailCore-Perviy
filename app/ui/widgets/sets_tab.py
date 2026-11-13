"""Вкладка «Формирование наборов»: сборка наборов (КИН) из товаров с ОСУ.

Для косметики и бытовой химии (`cosmetics`) и других групп с объёмно-сортовым
учётом (ОСУ) коды экземпляров не приходят в УПД при приёмке. Поэтому для
формирования набора кладовщик или менеджер:
1. Сканирует или вставляет заранее заказанный и нанесённый код набора (`КИН`).
2. Сканирует физические коды маркировки вложений (`КИ`) с единиц товара или
   загружает их из файла.
3. Проверяет статусы `КИН` и `КИ` в «Честном ЗНАКе» по правилам ОСУ.
4. Отправляет документ «Формирование упаковки» (`AGGREGATION_DOCUMENT`, JSON)
   или сохраняет его в файл.

Коды набора и вложений программа сама не генерирует. Черновик набора и номер
отправленного документа сохраняются на диск, чтобы при перезапуске не потерять
набранные позиции и не отправить один и тот же документ повторно.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.marking import codes as codes_module, introduce, service, sets
from ...core.marking.models import (
    CodeInfo,
    Contour,
    Operation,
    OperationKind,
    OperationStatus,
)
from ...core.marking.session import Organisation
from ...core.settings import AppSettings
from .. import icons
from ..tasks import run_task
from ..theme import Metrics, Palette
from .common import Card, Hint, MetricTile, SectionTitle
from .toast import ToastKind

DEFAULT_GROUP = sets.DEFAULT_PRODUCT_GROUP

GTIN_COLUMN = 1
SERIAL_COLUMN = 2
KI_COLUMN = 3
NAME_COLUMN = 4
STATE_COLUMN = 5
NOTE_COLUMN = 6

RESULT_COLOURS = {
    "added": (Palette.SUCCESS, Palette.SUCCESS_SOFT),
    "repeat": (Palette.WARNING, Palette.WARNING_SOFT),
    "broken": (Palette.DANGER, Palette.DANGER_SOFT),
}


class _NameEditor(QStyledItemDelegate):
    """Редактор наименования товара в строке таблицы."""

    def createEditor(self, parent, option, index):  # noqa: N802 — Qt
        editor = QLineEdit(parent)
        editor.setStyleSheet("QLineEdit { padding: 0 7px; margin: 0; border-radius: 0; }")
        return editor

    def updateEditorGeometry(self, editor, option, index):  # noqa: N802 — Qt
        editor.setGeometry(option.rect)


class SetsTab(QWidget):
    """Формирование наборов из товаров с ОСУ (`AGGREGATION_DOCUMENT`)."""

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
        draft_path: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._contour = contour
        self._thumbprint = thumbprint
        self._inn = inn
        self._organisations = organisations
        self._draft_path = draft_path
        self._filled_inn = ""
        self._refreshing = False
        self._restoring = False
        self._finding = False
        self._busy = False

        self.set_code: str = ""
        self.set_codes_pool: list[str] = []
        self._use_all_pool: bool = True
        self._resolved_group: str = ""
        self.codes: list[str] = []
        self._keys: set[str] = set()
        self._names: dict[str, str] = {}
        self._asked: set[str] = set()
        self._checks: dict[str, CodeInfo] = {}
        self._verification: sets.SetVerification | None = None
        self._operation_id: int = 0
        self._doc_id: str = ""
        self._doc_status: str = ""
        self._repeats = 0
        self._broken = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, Metrics.GAP, 0, 0)
        root.setSpacing(Metrics.GAP)
        root.addWidget(self._details_card())
        root.addWidget(self._codes_card(), 1)
        self._restore_draft()
        self._sync()

    # --- построение ----------------------------------------------------------------

    def _details_card(self) -> Card:
        card = Card(self)
        body = card.body()
        body.addWidget(SectionTitle("Реквизиты набора и код набора (КИН)", card))

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(9)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self.inn_box = QComboBox(card)
        self.inn_box.setEditable(True)
        self.inn_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.inn_box.setCompleter(None)
        self.inn_box.activated.connect(self._pick_organisation)
        self.inn_edit = self.inn_box.lineEdit()
        self.inn_edit.setPlaceholderText("ИНН организации или ИП")
        self.inn_edit.setMaxLength(12)
        self.inn_edit.textChanged.connect(self._on_details_changed)
        self.inn_box.setToolTip(
            "ИНН участника оборота (`participantId`). Подставляется из организации, "
            "под которой выполнен вход."
        )

        self.group_box = QComboBox(card)
        self.group_box.addItem("Косметика и бытовая химия", DEFAULT_GROUP)
        self.group_box.currentIndexChanged.connect(self._on_details_changed)
        self.group_box.setToolTip(
            "Товарная группа набора. По умолчанию — «Косметика и бытовая химия» (`cosmetics`)."
        )

        self.capacity_box = QSpinBox(card)
        self.capacity_box.setRange(0, introduce.MAX_PRODUCTS)
        self.capacity_box.setSpecialValueText("Не ограничивать")
        self.capacity_box.setValue(0)
        self.capacity_box.valueChanged.connect(self._on_details_changed)
        self.capacity_box.setToolTip(
            "Необязательный локальный предел количества вложений. "
            "Состав набора в ЧЗ определяется опубликованной карточкой товара."
        )

        set_row = QHBoxLayout()
        set_row.setSpacing(8)
        self.set_edit = QLineEdit(card)
        self.set_edit.setPlaceholderText(
            "Отсканируйте, вставьте или загрузите из CSV код идентификации набора (КИН)…"
        )
        self.set_edit.setMinimumHeight(36)
        self.set_edit.returnPressed.connect(self.accept_set_scan)
        self.set_edit.editingFinished.connect(self._on_set_edited)
        self.set_paste_button = self._button(
            card, "Вставить КИН", "copy", self.paste_set_code
        )
        self.set_file_button = self._button(
            card, "Загрузить CSV…", "open", self.load_set_file
        )
        self.set_clear_button = self._button(
            card, "Очистить КИН", "clear", self.clear_set_code
        )
        self.set_paste_button.setToolTip("Вставить код набора (КИН) из буфера обмена")
        self.set_file_button.setToolTip(
            "Загрузить код набора (КИН) или список КИН из CSV / текстового файла"
        )
        self.set_clear_button.setToolTip("Очистить код набора (КИН)")
        set_row.addWidget(self.set_edit, 1)
        set_row.addWidget(self.set_paste_button)
        set_row.addWidget(self.set_file_button)
        set_row.addWidget(self.set_clear_button)

        self.set_pool_widget = QWidget(card)
        pool_row = QHBoxLayout(self.set_pool_widget)
        pool_row.setContentsMargins(0, 0, 0, 0)
        pool_row.setSpacing(8)
        self.set_pool_label = QLabel("КИН из CSV:", self.set_pool_widget)
        self.set_pool_box = QComboBox(self.set_pool_widget)
        self.set_pool_box.setToolTip("Выбор кода набора (КИН) из загруженного файла CSV")
        self.set_pool_box.currentIndexChanged.connect(self._pick_pool_set_code)
        self.set_next_button = self._button(
            self.set_pool_widget, "Следующий КИН", "refresh", self.next_set_code
        )
        self.set_next_button.setToolTip("Переключить на следующий КИН из загруженного CSV")
        pool_row.addWidget(self.set_pool_label)
        pool_row.addWidget(self.set_pool_box, 1)
        pool_row.addWidget(self.set_next_button)
        self.set_pool_widget.hide()

        for combo in (self.inn_box, self.group_box, self.set_pool_box):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(12)

        grid.addWidget(QLabel("ИНН участника", card), 0, 0)
        grid.addWidget(self.inn_box, 0, 1)
        grid.addWidget(QLabel("Товарная группа", card), 0, 2)
        grid.addWidget(self.group_box, 0, 3)
        grid.addWidget(QLabel("Код набора (КИН)", card), 1, 0)
        grid.addLayout(set_row, 1, 1, 1, 3)
        grid.addWidget(self.set_pool_widget, 2, 1, 1, 3)
        grid.addWidget(QLabel("Максимум вложений", card), 3, 0)
        grid.addWidget(self.capacity_box, 3, 1)
        self.set_status_label = QLabel("КИН не задан", card)
        self.set_status_label.setWordWrap(True)
        grid.addWidget(QLabel("Сведения о КИН", card), 3, 2)
        grid.addWidget(self.set_status_label, 3, 3)
        body.addLayout(grid)

        self.details_hint = Hint(
            "ОСУ («Косметика и бытовая химия»): при приёмке по ОСУ коды экземпляров в УПД "
            "не передаются. Закажите в СУЗ код набора («Набор — КИН (SET)», способ выпуска "
            "«Производство в РФ»), подайте отчёт о нанесении (статус «Нанесён»), затем "
            "отсканируйте КИН и коды вложений (КИ) с товаров. Если вложения в статусе "
            "«В обороте», после обработки документа набор автоматически перейдёт в статус "
            "«В обороте», а вложения перейдут в собственность вашей организации.",
            card,
        )
        body.addWidget(self.details_hint)
        return card

    def _codes_card(self) -> Card:
        card = Card(self)
        body = card.body()

        header = QHBoxLayout()
        header.setSpacing(9)
        header.addWidget(SectionTitle("Вложения набора (КИ)", card))
        header.addStretch(1)
        self.paste_button = self._button(card, "Вставить", "copy", self.paste_codes)
        self.file_button = self._button(card, "Загрузить CSV…", "open", self.load_file)
        self.remove_button = self._button(card, "Убрать", "trash", self.remove_selected)
        self.remove_gtin_button = self._button(
            card, "Убрать этот GTIN", "trash", self.remove_selected_gtin
        )
        self.clear_button = self._button(card, "Очистить всё", "clear", self.clear)
        self.paste_button.setToolTip("Вставить коды вложений (КИ) из буфера обмена")
        self.file_button.setToolTip(
            "Загрузить коды маркировки продукции в наборе (КИ вложений) из CSV или текстового файла"
        )
        self.remove_button.setToolTip("Убрать выбранные в таблице коды вложений")
        self.remove_gtin_button.setToolTip(
            "Убрать из списка все коды вложений с тем же GTIN, что у выбранной строки"
        )
        self.clear_button.setToolTip("Очистить КИН и список вложений набора")
        for button in (
            self.paste_button,
            self.file_button,
            self.remove_button,
            self.remove_gtin_button,
            self.clear_button,
        ):
            header.addWidget(button)
        body.addLayout(header)

        self.scan_edit = QLineEdit(card)
        self.scan_edit.setPlaceholderText(
            "Наведите сканер на марку товара-вложения (КИ) — код добавится в набор"
        )
        self.scan_edit.setMinimumHeight(38)
        self.scan_edit.returnPressed.connect(self.accept_scan)
        body.addWidget(self.scan_edit)

        self.verdict_label = QLabel("", card)
        self.verdict_label.setWordWrap(True)
        self.verdict_label.setMinimumHeight(46)
        self.verdict_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self.verdict_label.hide()
        body.addWidget(self.verdict_label)

        tiles = QHBoxLayout()
        tiles.setSpacing(Metrics.GAP)
        self.tile_set = MetricTile("КИН набора", Palette.PRIMARY, card)
        self.tile_items = MetricTile("Вложений (КИ)", Palette.PRIMARY, card)
        self.tile_gtins = MetricTile("Товаров (GTIN)", Palette.PRIMARY, card)
        self.tile_repeats = MetricTile("Повторов", Palette.WARNING, card)
        self.tile_broken = MetricTile("Не принято", Palette.DANGER, card)
        for tile in (
            self.tile_set,
            self.tile_items,
            self.tile_gtins,
            self.tile_repeats,
            self.tile_broken,
        ):
            tiles.addWidget(tile, 1)
        body.addLayout(tiles)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(12)
        self.sound_box = QCheckBox("Звук при повторе и ошибке", card)
        self.sound_box.setChecked(self.settings.marking_reconcile_sound)
        controls_row.addWidget(self.sound_box)
        self.composition_label = Hint("Состав набора пока пуст.", card)
        controls_row.addWidget(self.composition_label, 1)
        body.addLayout(controls_row)

        self.table = QTableWidget(0, 7, card)
        self.table.setHorizontalHeaderLabels([
            "№",
            "Код товара (GTIN)",
            "Серийный номер",
            "Код идентификации (КИ)",
            "Наименование",
            "Статус в ЧЗ",
            "Владелец / ОСУ и замечания",
        ])
        self.table.setItemDelegateForColumn(NAME_COLUMN, _NameEditor(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.table.setMinimumHeight(150)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(NAME_COLUMN, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(NOTE_COLUMN, QHeaderView.ResizeMode.Stretch)
        for column in (0, GTIN_COLUMN, SERIAL_COLUMN, KI_COLUMN, STATE_COLUMN):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._sync)
        self.table.itemChanged.connect(self._cell_edited)
        body.addWidget(self.table, 1)

        actions = QHBoxLayout()
        actions.setSpacing(9)
        self.hint = Hint("", card)
        actions.addWidget(self.hint, 1)
        self.check_button = self._button(
            card, "Проверить коды в ЧЗ", "marking", self.check_codes_in_gis
        )
        self.poll_button = self._button(
            card, "Статус документа", "refresh", self.poll_document_status
        )
        self.file_save_button = self._button(
            card, "Сохранить JSON…", "save", self.save_file
        )
        self.send_button = self._button(
            card, "Сформировать набор в ЧЗ", "export", self.create_document
        )
        self.send_button.setObjectName("Primary")
        for button in (
            self.check_button,
            self.poll_button,
            self.file_save_button,
            self.send_button,
        ):
            actions.addWidget(button)
        body.addLayout(actions)
        return card

    @staticmethod
    def _button(
        parent: QWidget,
        text: str,
        icon: str,
        handler: Callable[[], None],
    ) -> QPushButton:
        button = QPushButton(text, parent)
        button.setIcon(icons.icon(icon))
        button.clicked.connect(lambda _checked=False: handler())
        return button

    # --- сканирование КИН и вложений -----------------------------------------------

    def focus_scan(self) -> None:
        """Переводит фокус в поле КИН (если он ещё не задан) или в поле вложений."""
        if not self.set_code.strip():
            self.set_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.scan_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def accept_set_scan(self) -> None:
        """Принимает отсканированный в поле КИН код набора и переводит курсор к вложениям."""
        self._use_all_pool = False
        if self.set_set_code(self.set_edit.text()) == "added":
            self._sync_set_pool()
            self._refresh_table_statuses()
            self.scan_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_set_edited(self) -> None:
        if self._restoring:
            return
        typed = self.set_edit.text().strip()
        if typed != self.set_code:
            self._use_all_pool = False
            self.set_set_code(typed, quiet=True)
            self._sync_set_pool()
            self._refresh_table_statuses()

    def paste_set_code(self) -> None:
        text = QApplication.clipboard().text()
        lines = codes_module.split_lines(text)
        if not lines:
            self._tell("В буфере обмена нет кода набора (КИН)", ToastKind.WARNING)
            return
        if len(lines) > 1:
            self.load_set_codes(text)
            return
        self._use_all_pool = False
        self.set_set_code(lines[0])
        self._sync_set_pool()
        self._refresh_table_statuses()

    def load_set_file(self, path: str = "") -> int:
        """Загружает КИН набора (один или список КИН) из файла CSV или TXT."""
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Файл CSV с кодами маркировки наборов (КИН)",
                "",
                "Файлы CSV и текст (*.csv *.txt);;CSV (*.csv);;Текст (*.txt);;Все файлы (*.*)",
            )
        if not path:
            return 0
        try:
            text = codes_module.read_codes_file(path)
        except OSError as error:
            self._tell(f"Не удалось прочитать файл КИН: {error}", ToastKind.ERROR)
            return 0
        return self.load_set_codes(text)

    def load_set_codes(self, text: str) -> int:
        """Разбирает один или несколько КИН из текста/CSV и включает режим партии при N > 1."""
        lines = codes_module.split_lines(text)
        if not lines:
            self._tell("В выбранном файле не найдено кодов наборов (КИН)", ToastKind.WARNING)
            return 0

        valid_codes: list[str] = []
        seen_keys: set[str] = set()
        repeats = 0
        broken = 0
        first_problem = ""

        for raw in lines:
            cleaned = codes_module.from_keyboard((raw or "").strip())
            parsed = codes_module.parse(cleaned)
            if not cleaned or not parsed.valid or not parsed.ki:
                broken += 1
                if not first_problem:
                    first_problem = parsed.problems[0] if parsed.problems else "неверный формат КИН"
                continue
            key = sets.key_of(cleaned)
            if key in self._keys or key in seen_keys:
                repeats += 1
                continue
            seen_keys.add(key)
            valid_codes.append(cleaned)

        self._repeats += repeats
        self._broken += broken

        if not valid_codes:
            reason = first_problem or "все коды уже добавлены как вложения"
            self._say("broken", f"КИН из файла не приняты: {reason}")
            self._sync()
            return 0

        self.set_codes_pool = valid_codes if len(valid_codes) > 1 else []
        self._use_all_pool = len(valid_codes) > 1
        self.set_set_code(valid_codes[0], quiet=True)
        self._sync_set_pool()
        self._refresh_table_statuses()

        parsed_first = codes_module.parse(valid_codes[0])
        tail: list[str] = []
        if repeats:
            tail.append(f"повторов {repeats}")
        if broken:
            tail.append(f"не принято {broken}")
        suffix = (" · " + ", ".join(tail)) if tail else ""
        if len(valid_codes) == 1:
            self._say(
                "added",
                f"КИН набора загружен · {parsed_first.gtin} · {parsed_first.serial}{suffix}",
            )
        else:
            self._say(
                "added",
                f"Загружено КИН наборов: {len(valid_codes)} шт. (партия GTIN {parsed_first.gtin})"
                f" · вложения распределятся по {len(valid_codes)} наборам{suffix}",
            )
        self._save_draft()
        self._sync()
        return len(valid_codes)

    def _sync_set_pool(self) -> None:
        has_pool = len(self.set_codes_pool) > 1
        self.set_pool_widget.setVisible(has_pool)
        self.set_pool_box.blockSignals(True)
        self.set_pool_box.clear()
        current_key = sets.key_of(self.set_code) if self.set_code.strip() else ""
        selected_idx = 0
        if has_pool:
            self.set_pool_box.addItem(
                f"Все КИН из CSV — партия ({len(self.set_codes_pool)} наборов)",
                "__ALL__",
            )
        for idx, code in enumerate(self.set_codes_pool):
            parsed = codes_module.parse(code)
            label = f"Только №{idx + 1} · GTIN {parsed.gtin} · сер. {parsed.serial}"
            self.set_pool_box.addItem(label, code)
            if not self._use_all_pool and current_key and sets.key_of(code) == current_key:
                selected_idx = idx + 1
        if has_pool:
            self.set_pool_box.setCurrentIndex(selected_idx)
            self.set_pool_label.setText(f"Режим КИН ({len(self.set_codes_pool)} шт.):")
        self.set_pool_box.blockSignals(False)

    def _pick_pool_set_code(self, index: int) -> None:
        if self._restoring or index < 0:
            return
        data = self.set_pool_box.itemData(index)
        if data == "__ALL__":
            self._use_all_pool = True
            if self.set_codes_pool:
                self.set_set_code(self.set_codes_pool[0], quiet=True)
            self._verification = None
            self._refresh_table_statuses()
            self._save_draft()
            self._sync()
            return
        if isinstance(data, str) and data:
            self._use_all_pool = False
            self.set_set_code(data)
            self._refresh_table_statuses()

    def next_set_code(self) -> bool:
        """Переключает активный КИН на следующий одиночный КИН из загруженного пула CSV."""
        if len(self.set_codes_pool) <= 1:
            return False
        self._use_all_pool = False
        current_key = sets.key_of(self.set_code) if self.set_code.strip() else ""
        keys = [sets.key_of(c) for c in self.set_codes_pool]
        try:
            idx = keys.index(current_key)
            next_idx = (idx + 1) % len(self.set_codes_pool)
        except ValueError:
            next_idx = 0
        chosen = self.set_codes_pool[next_idx]
        result = self.set_set_code(chosen)
        self._sync_set_pool()
        self._refresh_table_statuses()
        return result == "added"

    def clear_set_code(self) -> None:
        self.set_code = ""
        self.set_codes_pool.clear()
        self._use_all_pool = True
        self.set_edit.blockSignals(True)
        self.set_edit.clear()
        self.set_edit.blockSignals(False)
        self._sync_set_pool()
        self._verification = None
        self._refresh_table_statuses()
        self._save_draft()
        self._sync()
        self.set_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_set_code(self, raw_text: str, *, quiet: bool = False) -> str:
        """Устанавливает код идентификации набора (КИН)."""
        cleaned = codes_module.from_keyboard((raw_text or "").strip())
        if not cleaned:
            self.set_code = ""
            self.set_edit.blockSignals(True)
            self.set_edit.clear()
            self.set_edit.blockSignals(False)
            self._verification = None
            self._save_draft()
            self._sync()
            return "empty"

        parsed = codes_module.parse(cleaned)
        if not parsed.valid or not parsed.ki:
            self._broken += 1
            reason = parsed.problems[0] if parsed.problems else "неверный формат КИН"
            if not quiet:
                self._say("broken", f"КИН не принят: {reason}")
            self._sync()
            return "broken"

        key = sets.key_of(cleaned)
        if key in self._keys:
            self._repeats += 1
            if not quiet:
                self._say("broken", f"Этот код уже добавлен как вложение · {parsed.ki}")
            self._sync()
            return "repeat"

        self.set_code = cleaned
        self.set_edit.blockSignals(True)
        self.set_edit.setText(cleaned)
        self.set_edit.blockSignals(False)
        self._verification = None
        if not quiet:
            self._say("added", f"КИН набора принят · {parsed.gtin} · {parsed.serial}")
        self._save_draft()
        self._sync()
        self._find_names()
        return "added"

    def accept_scan(self) -> None:
        """Принимает один код вложения из поля сканирования."""
        text = self.scan_edit.text().strip()
        self.scan_edit.clear()
        if text:
            self.add_code(text)

    def add_code(self, text: str, *, quiet: bool = False) -> str:
        """Добавляет код вложения (`КИ`) в набор. Возвращает `added`, `repeat` или `broken`."""
        text = codes_module.from_keyboard((text or "").strip())
        parsed = codes_module.parse(text)
        if not text or not parsed.valid or not parsed.ki:
            self._broken += 1
            reason = parsed.problems[0] if parsed.problems else "код не разобран"
            if not quiet:
                self._say("broken", f"Не принято: {reason}")
            self._sync()
            return "broken"

        key = sets.key_of(text)
        active_set_keys = {sets.key_of(sc) for sc in self.active_set_codes}
        if (self.set_code and key == sets.key_of(self.set_code)) or key in active_set_keys:
            self._repeats += 1
            if not quiet:
                self._say(
                    "repeat",
                    f"Совпадает с кодом набора (КИН) · {parsed.gtin} · {parsed.serial}",
                )
            self._sync()
            return "repeat"

        if key in self._keys:
            self._repeats += 1
            if not quiet:
                self._say("repeat", f"Уже в наборе · {parsed.gtin} · {parsed.serial}")
            self._sync()
            return "repeat"

        self._keys.add(key)
        self.codes.append(text)
        self._verification = None
        self._append_row(len(self.codes) - 1, text)
        if not quiet:
            self._say("added", f"Вложение принято · {parsed.gtin} · {parsed.serial}")
            self.table.scrollToBottom()
        self._save_draft()
        self._sync()
        self._find_names()
        return "added"

    def _append_row(self, row: int, raw_code: str) -> None:
        parsed = codes_module.parse(raw_code)
        ki = parsed.ki or raw_code.strip()
        info = self._checks.get(ki)
        state_text, note_text, state_colour = self._row_status_cells(ki, info)

        self.table.insertRow(row)
        self._refreshing = True
        try:
            readonly_cells = (
                (0, str(row + 1)),
                (GTIN_COLUMN, parsed.gtin),
                (SERIAL_COLUMN, parsed.serial),
                (KI_COLUMN, ki),
                (STATE_COLUMN, state_text),
                (NOTE_COLUMN, note_text),
            )
            for column, value in readonly_cells:
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == STATE_COLUMN and state_colour:
                    item.setForeground(QColor(state_colour))
                self.table.setItem(row, column, item)
            name = (info.product_name if info and info.product_name else "") or self._name_of(
                parsed.gtin
            )
            self.table.setItem(row, NAME_COLUMN, QTableWidgetItem(name))
        finally:
            self._refreshing = False

    def _assigned_set_map(self) -> dict[str, str]:
        if not self.batch_mode:
            return {}
        units, batch_problems = self.units_and_problems()
        if batch_problems or len(units) <= 1:
            return {}
        mapping: dict[str, str] = {}
        for idx, unit in enumerate(units, start=1):
            label = f"Набор №{idx} ({unit.set_serial or unit.set_ki})"
            for item_ki in unit.item_kis:
                mapping[item_ki] = label
        return mapping

    def _row_status_cells(
        self,
        ki: str,
        info: CodeInfo | None,
        assigned_set: str = "",
    ) -> tuple[str, str, str]:
        if info is None:
            return "не проверен", assigned_set, Palette.TEXT_MUTED
        if not info.found:
            note = " · ".join(p for p in (assigned_set, "нет в ГИС МТ") if p)
            return "Не найден", note, Palette.DANGER
        if not info.valid:
            note = " · ".join(p for p in (assigned_set, "; ".join(info.problems)) if p)
            return "Недействителен", note, Palette.DANGER

        state_text = info.state.title
        colour = (
            Palette.SUCCESS
            if info.state.value in ("introduced", "applied")
            else Palette.DANGER
        )
        notes: list[str] = []
        if assigned_set:
            notes.append(assigned_set)
        if info.owner_name or info.owner_inn:
            owner = info.owner_name or info.owner_inn
            notes.append(f"Владелец: {owner}")
        if self._verification and ki in self._verification.code_notes:
            notes.append(self._verification.code_notes[ki])
        elif info.remarks:
            notes.extend(info.remarks)
        return state_text, " · ".join(notes), colour

    def _say(self, kind: str, text: str) -> None:
        colour, background = RESULT_COLOURS[kind]
        self.verdict_label.setText(text)
        self.verdict_label.show()
        self.verdict_label.setStyleSheet(
            f"color: {colour}; background: {background}; font-size: 18px;"
            f" font-weight: 700; border-radius: {Metrics.RADIUS}px;"
            " padding: 8px 14px;"
        )
        if kind != "added" and self.sound_box.isChecked():
            QApplication.beep()

    def paste_codes(self) -> None:
        self.add_many(QApplication.clipboard().text())

    def load_file(self, path: str | list[str] | tuple[str, ...] = "") -> tuple[int, int, int]:
        """Загружает коды маркировки вложений (КИ) из одного или нескольких файлов CSV/TXT."""
        if not path:
            paths, _ = QFileDialog.getOpenFileNames(
                self,
                "Файлы CSV с кодами маркировки вложений (КИ)",
                "",
                "Файлы CSV и текст (*.csv *.txt);;CSV (*.csv);;Текст (*.txt);;Все файлы (*.*)",
            )
        elif isinstance(path, str):
            paths = [path]
        else:
            paths = list(path)
        if not paths:
            return 0, 0, 0

        chunks: list[str] = []
        for item_path in paths:
            try:
                chunks.append(codes_module.read_codes_file(item_path))
            except OSError as error:
                self._tell(f"Не удалось прочитать файл {item_path}: {error}", ToastKind.ERROR)
                return 0, 0, 0
        added, repeats, broken = self.add_many("\n".join(chunks))
        if not (added or repeats or broken):
            self._tell("В выбранном файле не найдено кодов вложений (КИ)", ToastKind.WARNING)
        return added, repeats, broken

    def add_many(self, text: str) -> tuple[int, int, int]:
        """Добавляет коды вложений списком и показывает сводку."""
        added = repeats = broken = 0
        for line in codes_module.split_lines(text):
            result = self.add_code(line, quiet=True)
            added += result == "added"
            repeats += result == "repeat"
            broken += result == "broken"
        if added:
            self._refresh_table_statuses()
        if added or repeats or broken:
            tail = []
            if repeats:
                tail.append(f"повторов {repeats}")
            if broken:
                tail.append(f"не принято {broken}")
            self._say(
                "added" if added else "broken",
                f"Добавлено вложений: {added}"
                + (" · " + ", ".join(tail) if tail else ""),
            )
        return added, repeats, broken

    def remove_selected(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectionModel().selectedRows()},
            reverse=True,
        )
        for row in rows:
            self._keys.discard(sets.key_of(self.codes[row]))
            del self.codes[row]
            self.table.removeRow(row)
        self._verification = None
        self._renumber()
        self._refresh_table_statuses()
        self._save_draft()
        self._sync()

    def remove_selected_gtin(self) -> int:
        """Удаляет все коды вложений с GTIN выбранных строк таблицы."""
        selected_rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        target_gtins = {self._gtin_of(row) for row in selected_rows if self._gtin_of(row)}
        if not target_gtins:
            return 0
        rows_to_remove = [
            row
            for row in range(len(self.codes) - 1, -1, -1)
            if (codes_module.parse(self.codes[row]).gtin or self._gtin_of(row)) in target_gtins
        ]
        for row in rows_to_remove:
            self._keys.discard(sets.key_of(self.codes[row]))
            del self.codes[row]
            self.table.removeRow(row)
        self._verification = None
        self._renumber()
        self._refresh_table_statuses()
        self._save_draft()
        self._sync()
        return len(rows_to_remove)

    def _pending_submission(self) -> bool:
        if self._doc_id.strip():
            return True
        if self._operation_id:
            operation = service.store.get(self._operation_id)
            return bool(operation and operation.status.pending)
        return False

    def clear(self, *, force: bool = False) -> None:
        if self._pending_submission() and not force:
            self._tell(
                "У набора есть документ с неподтверждённым результатом. Проверьте его статус в ЧЗ перед очисткой.",
                ToastKind.WARNING,
            )
            return
        self.set_code = ""
        self.set_codes_pool.clear()
        self._use_all_pool = True
        self._resolved_group = ""
        self.set_edit.blockSignals(True)
        self.set_edit.clear()
        self.set_edit.blockSignals(False)
        self._sync_set_pool()
        self.codes.clear()
        self._keys.clear()
        self._checks.clear()
        self._verification = None
        self._operation_id = 0
        self._doc_id = ""
        self._doc_status = ""
        self.table.setRowCount(0)
        self._repeats = self._broken = 0
        self.verdict_label.setText("")
        self.verdict_label.setStyleSheet("")
        self.verdict_label.hide()
        sets.clear_draft(self._draft_path)
        self._sync()
        self.focus_scan()

    def _renumber(self) -> None:
        for row in range(self.table.rowCount()):
            item = QTableWidgetItem(str(row + 1))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, item)

    # --- наименования и предпросмотр состава ---------------------------------------

    def _gtin_of(self, row: int) -> str:
        item = self.table.item(row, GTIN_COLUMN)
        return item.text() if item is not None else ""

    def _cell_edited(self, item: QTableWidgetItem) -> None:
        if self._refreshing or item.column() != NAME_COLUMN:
            return
        gtin = self._gtin_of(item.row())
        text = item.text().strip()
        self._refreshing = True
        try:
            for row in range(self.table.rowCount()):
                other = self.table.item(row, NAME_COLUMN)
                if other is not None and row != item.row() and self._gtin_of(row) == gtin:
                    other.setText(text)
        finally:
            self._refreshing = False
        if gtin:
            self._names[gtin] = text
        self._save_draft()
        self._sync_composition()

    def _name_of(self, gtin: str) -> str:
        if gtin not in self._names:
            known = self.settings.marking_sale_items.get(gtin, {})
            if known.get("name"):
                self._names[gtin] = known["name"]
        return self._names.get(gtin, "")

    def _find_names(self) -> None:
        if self._finding:
            return
        wanted: dict[str, str] = {}
        if self.set_code:
            parsed_set = codes_module.parse(self.set_code)
            if parsed_set.gtin and not self._names.get(parsed_set.gtin) and parsed_set.gtin not in self._asked:
                wanted.setdefault(parsed_set.gtin, self.set_code)
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
            _look_up_names,
            list(wanted.values()),
            self._contour(),
            online,
            on_result=self._names_found,
            on_error=lambda _message: self._names_found({}),
        )

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
                    if self._gtin_of(row) == gtin and item is not None and not item.text().strip():
                        item.setText(name)
        finally:
            self._refreshing = False
        self._save_draft()
        self._sync_composition()
        self._find_names()

    @property
    def batch_mode(self) -> bool:
        """Режим формирования сразу всей загруженной из CSV партии наборов."""
        return self._use_all_pool and len(self.set_codes_pool) > 1

    @property
    def active_set_codes(self) -> tuple[str, ...]:
        if self.batch_mode:
            return tuple(self.set_codes_pool)
        if self.set_code.strip():
            return (self.set_code.strip(),)
        return ()

    def units_and_problems(self) -> tuple[tuple[sets.SetUnit, ...], list[str]]:
        capacity = self.capacity_box.value()
        return sets.build_units(
            self.active_set_codes,
            self.codes,
            capacity=capacity if capacity > 0 else None,
        )

    def unit(self) -> sets.SetUnit:
        units, _ = self.units_and_problems()
        return units[0]

    def document(self) -> sets.SetDocument:
        units, batch_problems = self.units_and_problems()
        return sets.SetDocument(
            inn=self.inn_edit.text().strip(),
            units=units,
            product_group=self.effective_group,
            batch_problems=tuple(batch_problems),
        )

    @property
    def group(self) -> str:
        return str(self.group_box.currentData() or DEFAULT_GROUP)

    @property
    def effective_group(self) -> str:
        """Код товарной группы для True API (`chemistry` или `cosmetics` по данным ЧЗ)."""
        base = self.group
        for raw_set in self.active_set_codes:
            set_ki = codes_module.parse(raw_set).ki
            set_info = self._checks.get(set_ki) or self._checks.get(raw_set.strip())
            if (
                set_info
                and set_info.product_group
                and sets.same_product_group(set_info.product_group, base)
            ):
                return set_info.product_group
        for raw in self.codes:
            ki = codes_module.parse(raw).ki or raw.strip()
            info = self._checks.get(ki) or self._checks.get(raw.strip())
            if (
                info
                and info.product_group
                and sets.same_product_group(info.product_group, base)
            ):
                return info.product_group
        if self._resolved_group and sets.same_product_group(self._resolved_group, base):
            return self._resolved_group
        return base

    # --- проверка кодов в ГИС МТ ---------------------------------------------------

    def check_codes_in_gis(self) -> None:
        """Запрашивает в ГИС МТ свежие статусы КИН и всех КИ вложений."""
        units, batch_problems = self.units_and_problems()
        if batch_problems:
            self._tell(f"Проверка не запущена: {batch_problems[0]}", ToastKind.WARNING)
            return
        for idx, unit in enumerate(units, start=1):
            if problems := unit.problems:
                prefix = f"набор №{idx}: " if len(units) > 1 else ""
                self._tell(f"Проверка не запущена: {prefix}{problems[0]}", ToastKind.WARNING)
                return
        if not service.signed_in():
            self._tell(
                "Для проверки кодов войдите по сертификату на вкладке «Проверка кодов»",
                ToastKind.WARNING,
            )
            return
        contour = self._contour()
        if service.current().contour != contour:
            self._tell("Вход выполнен в другом контуре — войдите заново", ToastKind.WARNING)
            return

        all_codes = [*self.active_set_codes, *self.codes]
        self._busy = True
        self._sync()
        self.hint.setStyleSheet("")
        self.hint.setText("Проверяем статусы КИН и вложений в «Честном ЗНАКе»…")
        run_task(
            _check_codes,
            all_codes,
            contour,
            on_result=self._on_codes_checked,
            on_error=self._on_check_error,
        )

    def _on_codes_checked(self, items: list[CodeInfo]) -> None:
        self._busy = False
        for item in items:
            if item.code:
                self._checks[item.code] = item
            parsed = codes_module.parse(item.code)
            if parsed.ki:
                self._checks[parsed.ki] = item
            if item.gtin and item.product_name and not self._names.get(item.gtin):
                self._names[item.gtin] = item.product_name

        units, batch_problems = self.units_and_problems()
        self._verification = sets.verify_units(
            units,
            self._checks,
            our_inn=self.inn_edit.text().strip() or (self._inn() or "").strip(),
            product_group=self.group,
            batch_problems=batch_problems,
        )
        self._refresh_table_statuses()
        self._save_draft()
        self._sync()

        if self._verification.problems:
            text = "Проверка выявила замечания: " + "; ".join(self._verification.problems[:5])
            if len(self._verification.problems) > 5:
                text += f" (и ещё {len(self._verification.problems) - 5})"
            self.hint.setStyleSheet(f"color: {Palette.DANGER};")
            self.hint.setText(text)
            self._tell(self._verification.problems[0], ToastKind.WARNING)
            return

        notes = list(self._verification.warnings) + list(self._verification.notes)
        summary = (
            "Все коды набора и вложений прошли проверку ЧЗ."
            + (" " + " ".join(notes) if notes else "")
        )
        self.hint.setStyleSheet(f"color: {Palette.SUCCESS};")
        self.hint.setText(summary)
        self._tell("Коды набора и вложений проверены в ЧЗ — ошибок нет", ToastKind.SUCCESS)

    def _on_check_error(self, message: str) -> None:
        self._busy = False
        self._sync()
        self.hint.setStyleSheet(f"color: {Palette.DANGER};")
        self.hint.setText(f"Не удалось проверить коды в ЧЗ: {message}")
        self._tell(f"Не удалось проверить коды: {message}", ToastKind.ERROR)

    def _refresh_table_statuses(self) -> None:
        assigned = self._assigned_set_map()
        self._refreshing = True
        try:
            for row, raw in enumerate(self.codes):
                parsed = codes_module.parse(raw)
                ki = parsed.ki or raw.strip()
                info = self._checks.get(ki)
                state_text, note_text, colour = self._row_status_cells(
                    ki, info, assigned.get(ki, "")
                )
                state_item = QTableWidgetItem(state_text)
                state_item.setFlags(state_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if colour:
                    state_item.setForeground(QColor(colour))
                note_item = QTableWidgetItem(note_text)
                note_item.setFlags(note_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, STATE_COLUMN, state_item)
                self.table.setItem(row, NOTE_COLUMN, note_item)
                name_item = self.table.item(row, NAME_COLUMN)
                if (
                    name_item is not None
                    and not name_item.text().strip()
                    and info is not None
                    and info.product_name
                ):
                    name_item.setText(info.product_name)
        finally:
            self._refreshing = False

    # --- реквизиты организации и черновик ------------------------------------------

    def prefill_inn(self) -> None:
        self._load_organisations()
        current = (self._inn() or "").strip()
        shown = self.inn_edit.text().strip()
        if current and (not shown or shown == self._filled_inn):
            self.inn_edit.setText(current)
            self._filled_inn = current

    def _load_organisations(self) -> None:
        known = list(self._organisations() or [])
        if (current := (self._inn() or "").strip()) and all(
            item.inn != current for item in known
        ):
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
        if inn := self.inn_box.itemData(index):
            self.inn_edit.setText(str(inn))

    def _on_details_changed(self, *_args) -> None:
        if self._restoring:
            return
        self._verification = None
        self._save_draft()
        self._sync()

    def _save_draft(self) -> None:
        if self._restoring:
            return
        if (
            not self.set_code.strip()
            and not self.set_codes_pool
            and not self.codes
            and not self._doc_id.strip()
        ):
            sets.clear_draft(self._draft_path)
            return
        draft = sets.SetDraft(
            inn=self.inn_edit.text().strip(),
            product_group=self.effective_group,
            set_code=self.set_code,
            set_codes_pool=list(self.set_codes_pool),
            use_all_pool=self._use_all_pool,
            capacity=self.capacity_box.value(),
            item_codes=list(self.codes),
            names=dict(self._names),
            operation_id=self._operation_id,
            doc_id=self._doc_id,
            doc_status=self._doc_status,
        )
        sets.save_draft(draft, self._draft_path)

    def _restore_draft(self) -> None:
        draft = sets.load_draft(self._draft_path)
        if not (
            draft.set_code
            or draft.set_codes_pool
            or draft.item_codes
            or draft.doc_id
            or draft.inn
        ):
            return
        self._restoring = True
        try:
            if draft.inn:
                self.inn_edit.setText(draft.inn)
                self._filled_inn = draft.inn
            if draft.product_group:
                self._resolved_group = draft.product_group
                idx = self.group_box.findData(draft.product_group)
                if idx < 0:
                    for i in range(self.group_box.count()):
                        if sets.same_product_group(
                            str(self.group_box.itemData(i) or ""), draft.product_group
                        ):
                            idx = i
                            break
                if idx >= 0:
                    self.group_box.setCurrentIndex(idx)
            self.capacity_box.setValue(max(0, int(draft.capacity or 0)))
            self._names.update(draft.names)
            self._operation_id = draft.operation_id
            self._doc_id = draft.doc_id
            self._doc_status = draft.doc_status
            self.set_codes_pool = [
                codes_module.from_keyboard(c.strip())
                for c in draft.set_codes_pool
                if c.strip()
            ]
            self._use_all_pool = bool(draft.use_all_pool)
            if draft.set_code:
                self.set_set_code(draft.set_code, quiet=True)
            self._sync_set_pool()
            for code in draft.item_codes:
                self.add_code(code, quiet=True)
            self._refresh_table_statuses()
            if self._doc_id:
                self.hint.setStyleSheet(f"color: {Palette.WARNING};")
                self.hint.setText(
                    f"Восстановлен черновик с отправленным документом {self._doc_id}"
                    + (f" ({self._doc_status})" if self._doc_status else "")
                    + ". Нажмите «Статус документа», чтобы узнать результат без повторной отправки."
                )
        finally:
            self._restoring = False

    # --- сохранение JSON и отправка в ЧЗ -------------------------------------------

    def save_file(self) -> None:
        """Сохраняет JSON `AGGREGATION_DOCUMENT` в файл без отправки в ГИС МТ."""
        document = self.document()
        if problems := document.problems:
            self._tell(f"Файл не собран: {problems[0]}", ToastKind.WARNING)
            return
        unit = self.unit()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить документ формирования набора",
            sets.file_name(unit.set_gtin),
            document.file_filters,
        )
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
        self._tell(f"Файл сохранён: {path}", ToastKind.SUCCESS)

    def create_document(self) -> None:
        """Подписывает и отправляет `AGGREGATION_DOCUMENT` в «Честный ЗНАК»."""
        if self._operation_id:
            previous = service.store.get(self._operation_id)
            if previous and previous.status in (OperationStatus.SENDING, OperationStatus.SENT):
                self._tell(
                    "Исход предыдущей отправки ещё не подтверждён. Проверьте документ в ЧЗ; повторная отправка заблокирована.",
                    ToastKind.WARNING,
                )
                return
        if self._doc_id:
            self._tell(
                f"По этому набору уже создан документ {self._doc_id}. "
                "Проверьте его кнопкой «Статус документа» или очистите черновик.",
                ToastKind.WARNING,
            )
            return
        document = self.document()
        if problems := document.problems:
            self._tell(f"Документ не отправлен: {problems[0]}", ToastKind.WARNING)
            return
        if self._verification and self._verification.problems:
            self._tell(
                f"Исправьте ошибки проверки ЧЗ перед отправкой: {self._verification.problems[0]}",
                ToastKind.WARNING,
            )
            return
        if not self._thumbprint():
            self._tell(
                "Документ подписывается — выберите сертификат на вкладке «Проверка кодов»",
                ToastKind.WARNING,
            )
            return
        if not service.signed_in():
            self._tell(
                "Отправка идёт от имени организации — войдите по сертификату на вкладке «Проверка кодов»",
                ToastKind.WARNING,
            )
            return
        contour = self._contour()
        if service.current().contour != contour:
            self._tell("Вход выполнен в другом контуре — войдите заново", ToastKind.WARNING)
            return

        unit = document.units[0]
        identifiers: list[str] = []
        for doc_unit in document.units:
            identifiers.append(doc_unit.set_ki)
            identifiers.extend(doc_unit.item_kis)
        earlier = service.warn_duplicates(
            Operation(kind=OperationKind.AGGREGATION, codes=identifiers)
        )
        if not self._confirm(document, contour, earlier):
            return

        group = self.effective_group
        if len(document.units) == 1:
            comment = f"КИН {unit.set_gtin} · вложений: {len(unit.item_codes)}"
        else:
            comment = (
                f"Партия {len(document.units)} наборов ({unit.set_gtin}) "
                f"· по {len(unit.item_codes)} вложений"
            )
        operation = service.start(
            OperationKind.AGGREGATION,
            identifiers,
            group=group,
            contour=contour,
            reason=sets.UNIT_TYPE_SET,
            comment=comment,
        )
        operation.status = OperationStatus.SENDING
        operation.sent_at = datetime.now()
        service.store.update(operation)
        self._operation_id = operation.id
        self._save_draft()
        self.journal_changed.emit()

        self._busy = True
        self._sync()
        self.hint.setStyleSheet("")
        self.hint.setText(
            "Подписываем и отправляем документ формирования набора — КриптоПро может спросить пароль…"
        )

        draft_file = self._draft_path
        def created(doc_id: str) -> None:
            operation.document_id = doc_id
            operation.status = OperationStatus.SENT
            service.store.update(operation)
            draft = sets.load_draft(draft_file)
            draft.doc_id = doc_id
            draft.doc_status = OperationStatus.SENT.title
            sets.save_draft(draft, draft_file)

        run_task(
            _send,
            document,
            self._thumbprint(),
            group,
            contour,
            created,
            on_result=lambda outcome: self._on_sent(operation, document, outcome),
            on_error=lambda message: self._on_send_error(operation, message),
        )

    def _confirm(
        self,
        document: sets.SetDocument,
        contour: Contour,
        earlier: list[Operation],
    ) -> bool:
        unit = document.units[0]
        comp = sets.composition(unit, checks=self._checks, names=self._names)
        n_sets = len(document.units)
        total_items = sum(len(u.item_codes) for u in document.units)
        item_parts = [
            f"{item.gtin}" + (f" ({item.name})" if item.name else "") + f" × {item.quantity} шт."
            for item in comp.items
        ]
        lines = [
            f"ИНН участника: {document.inn}.",
            f"Товарная группа: {self.group_box.currentText()} ({self.group}).",
        ]
        if n_sets == 1:
            lines.extend([
                f"КИН набора: {comp.set_ki}" + (f" ({comp.set_name})" if comp.set_name else "") + ".",
                f"Вложений в наборе: {comp.total_items} шт. (товарных позиций GTIN: {comp.distinct_gtins}).",
            ])
        else:
            lines.extend([
                f"Наборов (КИН) в документе: {n_sets} шт. (GTIN {comp.set_gtin}).",
                f"Вложений в каждом наборе: {comp.total_items} шт. (всего вложений в партии: {total_items} шт.).",
            ])
        if item_parts:
            lines.append("Состав 1 набора: " + "; ".join(item_parts) + ".")
        lines.extend([
            "Сверьте GTIN и количества вложений с опубликованной карточкой набора в Национальном каталоге.",
            f"Контур: {contour.title}.",
        ])
        if self._verification is None:
            lines.append(
                "\nКоды ещё не проверялись кнопкой «Проверить коды в ЧЗ». "
                "Рекомендуется проверить статусы КИН и вложений до отправки."
            )
        elif self._verification.notes:
            lines.append("\n" + " ".join(self._verification.notes))
        if earlier:
            when = earlier[0].created_at
            stamp = f" {when:%d.%m.%Y %H:%M}" if when else ""
            lines.append(
                f"\nВНИМАНИЕ: такой же набор кодов уже отправляли{stamp} "
                f"({earlier[0].status.title.lower()}). Скорее всего, это повторное нажатие."
            )
        lines.append(
            "\nДокумент AGGREGATION_DOCUMENT подписывается сертификатом и уходит "
            "в «Честный ЗНАК»."
        )
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Формирование набора")
        confirm.setIcon(QMessageBox.Icon.Warning)
        if n_sets == 1:
            confirm.setText(
                f"Сформировать набор {comp.set_gtin} из {comp.total_items} вложений?"
            )
        else:
            confirm.setText(
                f"Сформировать партию из {n_sets} наборов {comp.set_gtin} "
                f"по {comp.total_items} вложений в каждом (всего {total_items} КИ)?"
            )
        confirm.setInformativeText("\n".join(lines))
        yes = confirm.addButton(
            "Сформировать набор" if n_sets == 1 else f"Сформировать {n_sets} наборов",
            QMessageBox.ButtonRole.AcceptRole,
        )
        cancel = confirm.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        confirm.setDefaultButton(cancel)
        confirm.exec()
        return confirm.clickedButton() is yes

    def _on_sent(
        self,
        operation: Operation,
        document: sets.SetDocument,
        outcome: tuple,
    ) -> None:
        kind, payload = outcome
        self._busy = False
        if kind == "unknown":
            operation.status = OperationStatus.SENT
            operation.error = "исход неизвестен"
            service.store.update(operation)
            self._doc_status = "исход неизвестен"
            self._save_draft()
            self.journal_changed.emit()
            self._sync()
            self.hint.setStyleSheet(f"color: {Palette.WARNING};")
            self.hint.setText(payload)
            self._tell(payload, ToastKind.WARNING)
            return

        sent: introduce.Sent = payload
        status = sent.status
        operation.document_id = sent.doc_id
        self._doc_id = sent.doc_id
        self._doc_status = status.text

        if status.done:
            operation.status = OperationStatus.DONE
            operation.error = ""
            operation.checked_at = datetime.now()
            service.store.update(operation)
            self.journal_changed.emit()
            unit = document.units[0]
            n_sets = len(document.units)
            self._finish_completed_sets([u.set_code for u in document.units])
            if n_sets == 1:
                text = (
                    f"Набор {unit.set_gtin} ({len(unit.item_codes)} вложений) "
                    f"успешно сформирован в ЧЗ. Документ {sent.doc_id} обработан (CHECKED_OK)."
                )
            else:
                text = (
                    f"Партия из {n_sets} наборов {unit.set_gtin} "
                    f"(по {len(unit.item_codes)} вложений) успешно сформирована в ЧЗ. "
                    f"Документ {sent.doc_id} обработан (CHECKED_OK)."
                )
            self.hint.setStyleSheet(f"color: {Palette.SUCCESS};")
            self.hint.setText(text)
            self._tell(text, ToastKind.SUCCESS)
            return

        if status.failed:
            operation.status = OperationStatus.REJECTED
            operation.error = status.text
            operation.checked_at = datetime.now()
            service.store.update(operation)
            # Документ отклонён — освобождаем черновик для исправления и новой отправки.
            self._doc_id = ""
            self._save_draft()
            self.journal_changed.emit()
            self._sync()
            summary = self._format_rejection(status.text)
            text = f"Документ {sent.doc_id} отклонён: {summary}"
            self.hint.setStyleSheet(f"color: {Palette.DANGER};")
            self.hint.setText(text)
            self._tell(f"Набор не сформирован. {text}", ToastKind.ERROR)
            return

        operation.status = OperationStatus.SENT
        operation.error = status.text
        service.store.update(operation)
        self._save_draft()
        self.journal_changed.emit()
        self._sync()
        text = (
            f"Документ {sent.doc_id} принят ЧЗ, статус: {status.text}. "
            "Набор ещё не считается сформированным — нажмите «Статус документа» позже."
        )
        self.hint.setStyleSheet(f"color: {Palette.WARNING};")
        self.hint.setText(text)
        self._tell(text, ToastKind.WARNING)

    def _on_send_error(self, operation: Operation, message: str) -> None:
        self._busy = False
        if operation.document_id:
            self._doc_id = operation.document_id
            operation.status = OperationStatus.SENT
        else:
            operation.status = OperationStatus.FAILED
        operation.error = message
        service.store.update(operation)
        self._doc_status = message
        self._save_draft()
        self.journal_changed.emit()
        self._sync()
        self.hint.setStyleSheet(f"color: {Palette.DANGER};")
        if operation.document_id:
            warning = f"Документ {operation.document_id} создан, но его статус не подтверждён: {message}"
            self.hint.setText(warning)
            self._tell(warning, ToastKind.WARNING)
        else:
            self.hint.setText(f"Документ не отправлен: {message}")
            self._tell(f"Документ не отправлен: {message}", ToastKind.ERROR)

    # --- опрос статуса отправленного документа без повторной отправки --------------

    def poll_document_status(self) -> None:
        """Опрашивает статус сохранённого документа в ГИС МТ без повторной отправки."""
        doc_id = self._doc_id.strip()
        if not doc_id:
            self._tell("Нет отправленного документа, ожидающего проверки статуса", ToastKind.INFO)
            return
        if not service.signed_in():
            self._tell(
                "Для проверки статуса документа войдите по сертификату на вкладке «Проверка кодов»",
                ToastKind.WARNING,
            )
            return
        contour = self._contour()
        if service.current().contour != contour:
            self._tell("Вход выполнен в другом контуре — войдите заново", ToastKind.WARNING)
            return

        self._busy = True
        self._sync()
        self.hint.setStyleSheet("")
        self.hint.setText(f"Запрашиваем статус документа {doc_id} в «Честном ЗНАКе»…")
        run_task(
            introduce.status,
            doc_id,
            self.effective_group,
            contour,
            on_result=lambda status: self._on_polled(doc_id, status),
            on_error=self._on_poll_error,
        )

    def _on_polled(self, doc_id: str, status: introduce.DocStatus) -> None:
        self._busy = False
        self._doc_status = status.text
        if self._operation_id:
            op_state = (
                OperationStatus.DONE
                if status.done
                else (OperationStatus.REJECTED if status.failed else OperationStatus.SENT)
            )
            err = "" if status.done else status.text
            service.store.update_document_status(
                self._operation_id, doc_id, op_state, err, datetime.now()
            )
            self.journal_changed.emit()

        if status.done:
            units, _ = self.units_and_problems()
            unit = units[0]
            n_sets = len(units)
            self._finish_completed_sets([u.set_code for u in units])
            if n_sets == 1:
                text = (
                    f"Документ {doc_id} обработан (CHECKED_OK) — набор {unit.set_gtin} "
                    "сформирован в «Честном ЗНАКе»."
                )
            else:
                text = (
                    f"Документ {doc_id} обработан (CHECKED_OK) — партия из {n_sets} "
                    f"наборов {unit.set_gtin} сформирована в «Честном ЗНАКе»."
                )
            self.hint.setStyleSheet(f"color: {Palette.SUCCESS};")
            self.hint.setText(text)
            self._tell(text, ToastKind.SUCCESS)
            return

        if status.failed:
            self._doc_id = ""
            self._save_draft()
            self._sync()
            summary = self._format_rejection(status.text)
            text = f"Документ {doc_id} отклонён: {summary}"
            self.hint.setStyleSheet(f"color: {Palette.DANGER};")
            self.hint.setText(text)
            self._tell(text, ToastKind.ERROR)
            return

        self._save_draft()
        self._sync()
        text = f"Документ {doc_id}: {status.text}. Обработка ещё не завершена."
        self.hint.setStyleSheet(f"color: {Palette.WARNING};")
        self.hint.setText(text)
        self._tell(text, ToastKind.INFO)

    def _finish_completed_sets(self, used_set_codes: list[str]) -> None:
        used_keys = {sets.key_of(c) for c in used_set_codes if c.strip()}
        remaining = [
            code
            for code in self.set_codes_pool
            if sets.key_of(code) not in used_keys
        ]
        was_batch = self._use_all_pool
        self.clear(force=True)
        if remaining:
            self.set_codes_pool = remaining if len(remaining) > 1 else []
            self._use_all_pool = was_batch and len(remaining) > 1
            self.set_set_code(remaining[0], quiet=True)
            self._sync_set_pool()
            self._save_draft()
            self._sync()
            self.scan_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _format_rejection(self, status_text: str) -> str:
        """Сворачивает однотипные ошибки ЧЗ по партии наборов в понятное сообщение."""
        missing_gtins = sorted(
            set(
                re.findall(
                    r"134:\s*GTIN\s*(\d{14}),\s*предусмотренный иерархией",
                    status_text,
                )
            )
        )
        if missing_gtins:
            loaded_gtins = sorted(
                {codes_module.parse(c).gtin for c in self.codes if codes_module.parse(c).gtin}
            )
            loaded_part = (
                f" (сейчас загружены GTIN: {', '.join(loaded_gtins)})"
                if loaded_gtins
                else ""
            )
            return (
                f"код 134 — в карточке набора в Национальном каталоге указан обязательный "
                f"товар с GTIN {', '.join(missing_gtins)}, который отсутствует среди "
                f"загруженных вложений{loaded_part}. Замените неверный GTIN или добавьте "
                f"CSV с кодами для GTIN {', '.join(missing_gtins)}."
            )
        parts = [p.strip() for p in status_text.split(";") if p.strip()]
        if len(parts) > 3:
            return "; ".join(parts[:2]) + f" (и ещё ошибок: {len(parts) - 2})"
        return status_text

    def _on_poll_error(self, message: str) -> None:
        self._busy = False
        self._sync()
        self.hint.setStyleSheet(f"color: {Palette.WARNING};")
        self.hint.setText(f"Статус документа {self._doc_id} пока не получен: {message}")
        self._tell(f"Не удалось получить статус документа: {message}", ToastKind.ERROR)

    # --- синхронизация состояния интерфейса ----------------------------------------

    def _tell(self, text: str, kind: ToastKind) -> None:
        if self.notify:
            self.notify(text, kind)

    def _sync_composition(self) -> None:
        units, batch_problems = self.units_and_problems()
        unit = units[0]
        comp = sets.composition(unit, checks=self._checks, names=self._names)
        total_items = len(self.codes)
        distinct_gtins = len({codes_module.parse(c).gtin for c in self.codes if codes_module.parse(c).gtin})
        self.tile_items.set_value(total_items)
        self.tile_gtins.set_value(distinct_gtins)

        if not self.set_code.strip():
            self.tile_set.set_value("Не задан")
            self.set_status_label.setStyleSheet(f"color: {Palette.TEXT_MUTED};")
            self.set_status_label.setText("Отсканируйте, вставьте или загрузите из CSV КИН набора")
        elif self.batch_mode:
            n_sets = len(units)
            parsed_set = codes_module.parse(self.set_code)
            checked_sets = [
                self._checks.get(u.set_ki)
                for u in units
                if self._checks.get(u.set_ki) is not None
            ]
            has_set_errors = bool(
                self._verification
                and any(u.set_ki in self._verification.code_notes for u in units)
            )
            self.tile_set.set_value(f"{n_sets} шт.")
            if has_set_errors:
                self.set_status_label.setStyleSheet(f"color: {Palette.DANGER};")
                self.set_status_label.setText(
                    f"Партия: {n_sets} КИН (GTIN {parsed_set.gtin}) · есть замечания ЧЗ"
                )
            elif len(checked_sets) == n_sets:
                self.set_status_label.setStyleSheet(f"color: {Palette.SUCCESS};")
                self.set_status_label.setText(
                    f"Партия: {n_sets} КИН (GTIN {parsed_set.gtin}) · все проверены в ЧЗ"
                )
            else:
                self.set_status_label.setStyleSheet("")
                self.set_status_label.setText(
                    f"Партия из CSV: {n_sets} КИН (GTIN {parsed_set.gtin})"
                )
        else:
            parsed_set = codes_module.parse(self.set_code)
            if not parsed_set.valid or not parsed_set.ki:
                self.tile_set.set_value("Ошибка")
                self.set_status_label.setStyleSheet(f"color: {Palette.DANGER};")
                self.set_status_label.setText("Неверный формат GS1 у кода набора")
            else:
                set_info = self._checks.get(parsed_set.ki)
                if set_info is None:
                    self.tile_set.set_value("Готов")
                    self.set_status_label.setStyleSheet("")
                    label = f"GTIN {parsed_set.gtin} · сер. {parsed_set.serial}"
                    if comp.set_name:
                        label += f" · {comp.set_name}"
                    self.set_status_label.setText(label)
                else:
                    note = (
                        self._verification.code_notes.get(parsed_set.ki, "")
                        if self._verification
                        else ""
                    )
                    self.tile_set.set_value(set_info.state.title)
                    colour = (
                        Palette.DANGER
                        if note or not set_info.found or not set_info.valid
                        else Palette.SUCCESS
                    )
                    self.set_status_label.setStyleSheet(f"color: {colour};")
                    parts = [
                        f"GTIN {parsed_set.gtin} · сер. {parsed_set.serial}",
                        f"Статус ЧЗ: {set_info.state.title}",
                    ]
                    if set_info.emission_type:
                        parts.append(f"Выпуск: {set_info.emission_type}")
                    if set_info.package_type:
                        parts.append(f"Тип: {set_info.package_type}")
                    if note:
                        parts.append(note)
                    self.set_status_label.setText(" · ".join(parts))

        if not self.codes:
            self.composition_label.setText("Состав набора пока пуст.")
            return
        if batch_problems:
            self.composition_label.setText(batch_problems[0])
            return
        parts = []
        for item in comp.items:
            title = f"{item.gtin}" + (f" ({item.name})" if item.name else "")
            parts.append(f"{title} × {item.quantity} шт.")
        if len(units) > 1:
            self.composition_label.setText(
                f"Партия: {len(units)} наборов по {comp.total_items} вложения (всего {total_items} КИ). "
                "В каждом наборе: " + "; ".join(parts)
            )
        else:
            self.composition_label.setText("Состав набора: " + "; ".join(parts))

    def _sync(self) -> None:
        self._sync_composition()
        self.tile_repeats.set_value(self._repeats)
        self.tile_broken.set_value(self._broken)

        has_set = bool(self.set_code.strip())
        has_items = bool(self.codes)
        ready = has_set and has_items
        has_pending_doc = bool(self._doc_id.strip())
        locked = self._pending_submission()

        has_selection = bool(
            self.table.selectionModel() and self.table.selectionModel().hasSelection()
        )
        self.remove_button.setEnabled(has_selection and not self._busy and not locked)
        self.remove_gtin_button.setEnabled(has_selection and not self._busy and not locked)
        self.clear_button.setEnabled((has_set or has_items) and not self._busy and not locked)
        self.set_clear_button.setEnabled(has_set and not self._busy and not locked)
        self.check_button.setEnabled(ready and not self._busy and not locked)
        self.poll_button.setEnabled(has_pending_doc and not self._busy)
        self.file_save_button.setEnabled(ready and not self._busy)
        self.send_button.setEnabled(ready and not locked and not self._busy)

        for widget in (
            self.set_edit,
            self.set_paste_button,
            self.set_file_button,
            self.set_pool_box,
            self.set_next_button,
            self.scan_edit,
            self.paste_button,
            self.file_button,
            self.capacity_box,
            self.group_box,
            self.inn_box,
        ):
            widget.setEnabled(not self._busy and not locked)

        if not self._busy and not self.hint.text():
            self.hint.setStyleSheet("")
            self.hint.setText(
                "Отсканируйте КИН набора и марки вложений (КИ), проверьте статусы в ЧЗ "
                "и нажмите «Сформировать набор в ЧЗ»."
            )


def _check_codes(codes: list[str], contour: Contour) -> list[CodeInfo]:
    return service.check(codes, contour=contour, use_cache=False)


def _send(
    document: sets.SetDocument,
    thumbprint: str,
    group: str,
    contour: Contour,
    created: Callable[[str], None],
) -> tuple[str, object]:
    try:
        return "sent", introduce.send(
            document, thumbprint, group, contour, on_created=created
        )
    except introduce.IntroduceUnknown as error:
        return "unknown", str(error)


def _look_up_names(
    codes: list[str],
    contour: Contour,
    online: bool,
) -> dict[str, str]:
    requested = {code: codes_module.for_request(code) or code for code in codes}
    found: dict[str, str] = {}
    for info in service.store.known_checks(
        list(requested.values()), contour
    ).values():
        if info.product_name:
            found[info.gtin or codes_module.parse(info.code).gtin] = info.product_name
    missing = [code for code in codes if codes_module.parse(code).gtin not in found]
    if online and missing:
        for info in service.check(missing, contour=contour):
            gtin = info.gtin or codes_module.parse(info.code).gtin
            if gtin and info.product_name:
                found.setdefault(gtin, info.product_name)
    return found
