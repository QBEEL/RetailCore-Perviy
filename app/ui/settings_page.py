"""Настройки поиска, сопоставления и сохранения. Изменения применяются сразу."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import appdata, snapshots
from ..core.models import DEFAULT_SEARCH_ROLES, DEFAULT_WEIGHTS, FieldRole, Sheet
from ..core.settings import DEFAULT_FILL_ROLES, AppSettings
from . import icons
from .theme import Metrics, Palette
from .widgets.common import Card, Divider, Hint, SectionTitle, Subtitle, Title
from .widgets.inputs import DecimalInput, SelectBox
from .widgets.toast import ToastKind

# Поля, доступные для поиска. Порядок — по убыванию значимости.
_SEARCH_FIELDS: tuple[FieldRole, ...] = (
    FieldRole.ARTICLE, FieldRole.SKU, FieldRole.EAN, FieldRole.NAME, FieldRole.NAME_ALT,
    FieldRole.BRAND, FieldRole.CATEGORY, FieldRole.VOLUME, FieldRole.COLOR, FieldRole.SIZE,
    FieldRole.MANUFACTURER, FieldRole.DESCRIPTION, FieldRole.NOTE,
)
_FILL_FIELDS: tuple[FieldRole, ...] = (
    FieldRole.EAN, FieldRole.ARTICLE, FieldRole.SKU, FieldRole.PRICE,
    FieldRole.BRAND, FieldRole.CATEGORY, FieldRole.MANUFACTURER,
)


class SettingsPage(QWidget):
    """Все настройки в одном месте; сохраняются автоматически."""

    def __init__(
        self,
        settings: AppSettings,
        notify: Callable[[str, ToastKind], None],
        parent: QWidget | None = None,
        check_updates: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.notify = notify
        self._check_updates = check_updates
        self._role_boxes: dict[FieldRole, QCheckBox] = {}
        self._weight_boxes: dict[FieldRole, DecimalInput] = {}
        self._fill_boxes: dict[FieldRole, QCheckBox] = {}
        self._mapping_boxes: list[tuple[int, SelectBox]] = []
        self._mapping_sheet: Sheet | None = None
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Metrics.PAD + 6, Metrics.PAD + 2, Metrics.PAD + 6, Metrics.PAD)
        outer.setSpacing(Metrics.GAP)
        outer.addWidget(Title("Настройки", self))
        outer.addWidget(Subtitle("Изменения сохраняются автоматически и применяются к следующему поиску.", self))

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget(scroll)
        self._body = QVBoxLayout(content)
        self._body.setContentsMargins(0, 0, 8, 0)
        self._body.setSpacing(Metrics.GAP)

        self._body.addWidget(self._search_fields_card())
        self._body.addWidget(self._search_behaviour_card())
        self._body.addWidget(self._matching_card())
        self._body.addWidget(self._fill_card())
        self._body.addWidget(self._mapping_card())
        self._body.addWidget(self._history_card())
        self._body.addWidget(self._updates_card())
        self._body.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

    # --- разделы --------------------------------------------------------------

    def _search_fields_card(self) -> Card:
        card = Card(self)
        body = card.body()
        body.addWidget(SectionTitle("Где искать", card))
        body.addWidget(Hint(
            "Отметьте поля, по которым выполняется поиск. Вес задаёт важность поля: "
            "совпадение в поле с большим весом даёт более высокую оценку.", card))

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        grid.addWidget(_column_label("Поле", card), 0, 0)
        grid.addWidget(_column_label("Вес", card), 0, 1)
        grid.addWidget(_column_label("Поле", card), 0, 3)
        grid.addWidget(_column_label("Вес", card), 0, 4)
        grid.setColumnStretch(2, 1)
        grid.setColumnMinimumWidth(2, 24)

        half = (len(_SEARCH_FIELDS) + 1) // 2
        for index, role in enumerate(_SEARCH_FIELDS):
            row = index % half + 1
            column = 0 if index < half else 3

            box = QCheckBox(role.title, card)
            box.setChecked(role in self.settings.search.roles)
            box.toggled.connect(lambda checked, r=role: self._toggle_role(r, checked))
            self._role_boxes[role] = box
            grid.addWidget(box, row, column)

            weight = DecimalInput(card)
            weight.setRange(0.1, 2.0)
            weight.setSingleStep(0.05)
            weight.setDecimals(2)
            weight.setFixedWidth(78)
            weight.setValue(self.settings.search.weight(role))
            weight.valueChanged.connect(lambda value, r=role: self._set_weight(r, value))
            self._weight_boxes[role] = weight
            grid.addWidget(weight, row, column + 1)

        body.addLayout(grid)
        body.addWidget(Divider(card))

        buttons = QHBoxLayout()
        select_all = QPushButton("Отметить все", card)
        select_all.clicked.connect(lambda: self._set_all_roles(True))
        clear_all = QPushButton("Снять все", card)
        clear_all.clicked.connect(lambda: self._set_all_roles(False))
        restore = QPushButton("По умолчанию", card)
        restore.clicked.connect(self._restore_defaults)
        buttons.addWidget(select_all)
        buttons.addWidget(clear_all)
        buttons.addWidget(restore)
        buttons.addStretch(1)
        body.addLayout(buttons)
        return card

    def _search_behaviour_card(self) -> Card:
        card = Card(self)
        body = card.body()
        body.addWidget(SectionTitle("Нечёткий поиск", card))
        body.addWidget(Hint("Позволяет находить записи при опечатках и различиях в написании.", card))

        self.fuzzy_box = QCheckBox("Учитывать опечатки", card)
        self.fuzzy_box.setChecked(self.settings.search.fuzzy_enabled)
        self.fuzzy_box.toggled.connect(self._set_fuzzy)
        body.addWidget(self.fuzzy_box)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        self.fuzzy_threshold = _spin(card, 40, 100, self.settings.search.fuzzy_threshold, 1.0, 0)
        self.fuzzy_threshold.valueChanged.connect(self._set_fuzzy_threshold)
        grid.addWidget(QLabel("Порог схожести, %", card), 0, 0)
        grid.addWidget(self.fuzzy_threshold, 0, 1)
        grid.addWidget(Hint("Ниже порога записи не попадают в выдачу", card), 0, 2)

        self.min_score = _spin(card, 0, 100, self.settings.search.min_score, 1.0, 0)
        self.min_score.valueChanged.connect(self._set_min_score)
        grid.addWidget(QLabel("Минимальная оценка, %", card), 1, 0)
        grid.addWidget(self.min_score, 1, 1)
        grid.addWidget(Hint("Отсекает слабые совпадения в результатах", card), 1, 2)
        grid.setColumnStretch(2, 1)
        body.addLayout(grid)
        return card

    def _matching_card(self) -> Card:
        card = Card(self)
        body = card.body()
        body.addWidget(SectionTitle("Сопоставление", card))

        self.volume_box = QCheckBox("Требовать совпадения объёма", card)
        self.volume_box.setChecked(self.settings.match.enforce_volume)
        self.volume_box.setToolTip(
            "Не позволяет привязать строку на 50 мл к записи на 10 мл: такие варианты "
            "уходят в «Требует проверки» вместо автоматической подстановки.")
        self.volume_box.toggled.connect(self._set_enforce_volume)
        body.addWidget(self.volume_box)
        body.addWidget(Hint(
            "Рекомендуется оставить включённым: без этой проверки в файл могут попасть "
            "цена и артикул от другого объёма того же товара.", card))

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)

        self.tolerance = _spin(card, 0, 50, self.settings.match.volume_tolerance * 100, 1.0, 0)
        self.tolerance.valueChanged.connect(self._set_tolerance)
        grid.addWidget(QLabel("Допуск по объёму, %", card), 0, 0)
        grid.addWidget(self.tolerance, 0, 1)
        grid.addWidget(Hint("195 мл и 200 мл считаются одним товаром", card), 0, 2)

        self.match_threshold = _spin(card, 40, 100, self.settings.match.fuzzy_threshold, 1.0, 0)
        self.match_threshold.valueChanged.connect(self._set_match_threshold)
        grid.addWidget(QLabel("Порог по названию, %", card), 1, 0)
        grid.addWidget(self.match_threshold, 1, 1)
        grid.addWidget(Hint("Насколько названия должны быть похожи", card), 1, 2)

        self.auto_accept = _spin(card, 50, 100, self.settings.match.auto_accept, 1.0, 0)
        self.auto_accept.valueChanged.connect(self._set_auto_accept)
        grid.addWidget(QLabel("Принимать без проверки, %", card), 2, 0)
        grid.addWidget(self.auto_accept, 2, 1)
        grid.addWidget(Hint("Ниже этой оценки строка помечается «Требует проверки»", card), 2, 2)
        grid.setColumnStretch(2, 1)
        body.addLayout(grid)
        return card

    def _fill_card(self) -> Card:
        card = Card(self)
        body = card.body()
        body.addWidget(SectionTitle("Что записывать в целевой файл", card))
        body.addWidget(Hint("Значения берутся из каталога и записываются в одноимённые колонки цели.", card))

        row = QHBoxLayout()
        row.setSpacing(16)
        for role in _FILL_FIELDS:
            box = QCheckBox(role.title, card)
            box.setChecked(role in self.settings.fill_roles)
            box.toggled.connect(lambda checked, r=role: self._toggle_fill(r, checked))
            self._fill_boxes[role] = box
            row.addWidget(box)
        row.addStretch(1)
        body.addLayout(row)

        self.overwrite_box = QCheckBox("Перезаписывать уже заполненные ячейки", card)
        self.overwrite_box.setChecked(self.settings.overwrite_filled)
        self.overwrite_box.setToolTip("По умолчанию заполняются только пустые ячейки")
        self.overwrite_box.toggled.connect(self._set_overwrite)
        body.addWidget(self.overwrite_box)
        return card

    def _mapping_card(self) -> Card:
        card = Card(self)
        self._mapping_body = card.body()
        self._mapping_body.addWidget(SectionTitle("Колонки файла", card))
        self._mapping_hint = Hint(
            "Роли колонок определяются автоматически. Загрузите файл, чтобы изменить их вручную.", card)
        self._mapping_body.addWidget(self._mapping_hint)
        self._mapping_grid = QGridLayout()
        self._mapping_grid.setHorizontalSpacing(14)
        self._mapping_grid.setVerticalSpacing(6)
        self._mapping_body.addLayout(self._mapping_grid)
        return card

    def _history_card(self) -> Card:
        card = Card(self)
        body = card.body()
        body.addWidget(SectionTitle("История данных", card))
        body.addWidget(Hint(
            "Каждая загрузка каталога сохраняется целиком, чтобы можно было сравнить "
            "ассортимент и цены между периодами. Повторная загрузка того же файла "
            "новую версию не создаёт.", card))

        self.snapshots_box = QCheckBox("Сохранять снимки загрузок", card)
        self.snapshots_box.setChecked(self.settings.snapshots_enabled)
        self.snapshots_box.toggled.connect(self._set_snapshots)
        body.addWidget(self.snapshots_box)

        self.snapshots_info = Hint("", card)
        body.addWidget(self.snapshots_info)

        row = QHBoxLayout()
        open_folder = QPushButton("Открыть папку с данными", card)
        open_folder.setIcon(icons.icon("folder"))
        open_folder.clicked.connect(self._open_data_dir)
        row.addWidget(open_folder)
        row.addStretch(1)
        body.addLayout(row)
        self._refresh_snapshots_info()
        return card

    def _refresh_snapshots_info(self) -> None:
        try:
            count = len(snapshots.list_snapshots())
            size = snapshots.database_size() / 1024 / 1024
        except Exception as error:  # noqa: BLE001 — сводка не должна ломать настройки
            self.snapshots_info.setText(f"База истории недоступна: {error}")
            return
        self.snapshots_info.setText(
            f"Сохранено снимков: {count} · размер базы: {size:.1f} МБ"
            if count else "Снимков пока нет.")

    def _open_data_dir(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(appdata.data_dir()))

    def _updates_card(self) -> Card:
        card = Card(self)
        body = card.body()
        body.addWidget(SectionTitle("Обновления", card))
        body.addWidget(Hint(f"Установлена версия {__version__}.", card))

        auto_check = QCheckBox("Проверять обновления автоматически", card)
        auto_check.setChecked(self.settings.update_check_auto)
        auto_check.toggled.connect(self._set_update_check_auto)
        body.addWidget(auto_check)

        auto_download = QCheckBox("Загружать обновления автоматически", card)
        auto_download.setChecked(self.settings.update_download_auto)
        auto_download.toggled.connect(self._set_update_download_auto)
        body.addWidget(auto_download)

        show_changelog = QCheckBox("Показывать описание новых версий", card)
        show_changelog.setChecked(self.settings.update_show_changelog)
        show_changelog.toggled.connect(self._set_update_show_changelog)
        body.addWidget(show_changelog)

        if self._check_updates is not None:
            check_now = QPushButton("Проверить сейчас", card)
            check_now.clicked.connect(self._check_updates)
            body.addWidget(check_now, 0, Qt.AlignmentFlag.AlignLeft)
        return card

    def show_mapping(self, sheet: Sheet) -> None:
        """Показывает распознанные колонки активного файла и позволяет их поправить."""
        self._mapping_sheet = sheet
        self._clear_mapping()
        self._mapping_hint.setText(
            f"Файл: {sheet.path}  ·  лист «{sheet.sheet_name}»  ·  заголовок в строке {sheet.header_row + 1}")

        self._mapping_grid.addWidget(_column_label("Колонка", self), 0, 0)
        self._mapping_grid.addWidget(_column_label("Заголовок", self), 0, 1)
        self._mapping_grid.addWidget(_column_label("Роль", self), 0, 2)
        self._mapping_grid.setColumnStretch(1, 1)

        overrides = self.settings.overrides_for(sheet.path)
        for row, column in enumerate(sheet.columns, start=1):
            self._mapping_grid.addWidget(QLabel(column.letter, self), row, 0)
            title = QLabel(column.title, self)
            title.setStyleSheet(f"color: {Palette.TEXT_MUTED};")
            self._mapping_grid.addWidget(title, row, 1)

            combo = SelectBox(self)
            for role in FieldRole:
                combo.addItem(role.title, role)
            current = overrides.get(column.index, column.role)
            combo.setCurrentIndex(list(FieldRole).index(current))
            combo.currentIndexChanged.connect(
                lambda _, index=column.index, box=combo: self._set_override(index, box.currentData()))
            self._mapping_grid.addWidget(combo, row, 2)
            self._mapping_boxes.append((column.index, combo))

    def _clear_mapping(self) -> None:
        self._mapping_boxes.clear()
        while self._mapping_grid.count():
            item = self._mapping_grid.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()

    # --- обработчики ----------------------------------------------------------

    def _toggle_role(self, role: FieldRole, checked: bool) -> None:
        if checked:
            self.settings.search.roles.add(role)
        else:
            self.settings.search.roles.discard(role)
        self._save()

    def _set_weight(self, role: FieldRole, value: float) -> None:
        self.settings.search.weights[role] = value
        self._save()

    def _set_all_roles(self, checked: bool) -> None:
        for box in self._role_boxes.values():
            box.setChecked(checked)

    def _restore_defaults(self) -> None:
        for role, box in self._role_boxes.items():
            box.setChecked(role in DEFAULT_SEARCH_ROLES)
        for role, spin in self._weight_boxes.items():
            spin.setValue(DEFAULT_WEIGHTS.get(role, 0.5))
        for role, box in self._fill_boxes.items():
            box.setChecked(role in DEFAULT_FILL_ROLES)
        self.fuzzy_box.setChecked(True)
        self.fuzzy_threshold.setValue(75)
        self.min_score.setValue(35)
        self.volume_box.setChecked(True)
        self.tolerance.setValue(5)
        self.match_threshold.setValue(72)
        self.auto_accept.setValue(90)
        self.notify("Настройки сброшены к значениям по умолчанию", ToastKind.INFO)

    def _set_fuzzy(self, checked: bool) -> None:
        self.settings.search.fuzzy_enabled = checked
        self._save()

    def _set_fuzzy_threshold(self, value: float) -> None:
        self.settings.search.fuzzy_threshold = value
        self._save()

    def _set_min_score(self, value: float) -> None:
        self.settings.search.min_score = value
        self._save()

    def _set_enforce_volume(self, checked: bool) -> None:
        self.settings.match.enforce_volume = checked
        self._save()
        if not checked:
            self.notify("Проверка объёма отключена — результаты требуют внимательной сверки", ToastKind.WARNING)

    def _set_tolerance(self, value: float) -> None:
        self.settings.match.volume_tolerance = value / 100.0
        self._save()

    def _set_match_threshold(self, value: float) -> None:
        self.settings.match.fuzzy_threshold = value
        self._save()

    def _set_auto_accept(self, value: float) -> None:
        self.settings.match.auto_accept = value
        self._save()

    def _toggle_fill(self, role: FieldRole, checked: bool) -> None:
        if checked and role not in self.settings.fill_roles:
            self.settings.fill_roles.append(role)
        elif not checked and role in self.settings.fill_roles:
            self.settings.fill_roles.remove(role)
        self._save()

    def _set_overwrite(self, checked: bool) -> None:
        self.settings.overwrite_filled = checked
        self._save()

    def _set_override(self, index: int, role: FieldRole) -> None:
        if self._mapping_sheet is None:
            return
        path = self._mapping_sheet.path
        overrides = dict(self.settings.overrides_for(path))
        overrides[index] = role
        self.settings.set_overrides(path, overrides)
        self._save()

    def _set_snapshots(self, checked: bool) -> None:
        self.settings.snapshots_enabled = checked
        self._save()
        if not checked:
            self.notify("Новые загрузки больше не сохраняются в историю", ToastKind.WARNING)

    def _set_update_check_auto(self, checked: bool) -> None:
        self.settings.update_check_auto = checked
        self._save()

    def _set_update_download_auto(self, checked: bool) -> None:
        self.settings.update_download_auto = checked
        self._save()

    def _set_update_show_changelog(self, checked: bool) -> None:
        self.settings.update_show_changelog = checked
        self._save()

    def _save(self) -> None:
        self.settings.save()


def _spin(parent: QWidget, minimum: float, maximum: float, value: float,
          step: float, decimals: int) -> DecimalInput:
    box = DecimalInput(parent)
    box.setRange(minimum, maximum)
    box.setSingleStep(step)
    box.setDecimals(decimals)
    box.setValue(value)
    box.setFixedWidth(96)
    return box


def _column_label(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    label.setStyleSheet(f"color: {Palette.TEXT_FAINT}; font-size: 11px; font-weight: 600;")
    label.setAlignment(Qt.AlignmentFlag.AlignLeft)
    return label
