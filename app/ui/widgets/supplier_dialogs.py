"""Диалог структуры прайса: добавить или поправить разбор файла поставщика.

Структура появляется сама после первого сравнения, но этого мало в двух
случаях. Поставщика заводят заранее, ещё до переоценки. И — важнее — поставщик
присылает файл нового формата: без заранее заданной структуры приложение не
узнает его и заведёт вторую карточку вместо того, чтобы дополнить имеющуюся.

Здесь же впервые правится разметка колонок: автоопределение ошибается на
нестандартных прайсах, а роль колонки решает, по чему вообще искать товар.
"""
from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ...core import pricing, suppliers, workbook
from ...core.models import FieldRole
from ...core.pricing import SupplierPrice, SupplierProfile
from ...core.suppliers import Supplier, SupplierLayout
from ..tasks import run_task
from ..theme import Metrics, Palette
from .common import Divider, Hint, SectionTitle
from .file_picker import FilePicker
from .inputs import SelectBox

# Роли, которые имеет смысл назначать вручную: по ним ищется товар.
_ROLES: tuple[FieldRole, ...] = (
    FieldRole.ARTICLE,
    FieldRole.SKU,
    FieldRole.EAN,
    FieldRole.NAME,
    FieldRole.NAME_ALT,
    FieldRole.CATEGORY,
    FieldRole.VOLUME,
    FieldRole.BRAND,
)
_AUTO = "— определять автоматически —"
_NONE = "— не заполнять —"


class SupplierLayoutDialog(QDialog):
    """Файл поставщика, роли его колонок и соответствие видам цен 1С."""

    def __init__(
        self,
        supplier: Supplier,
        layout: SupplierLayout | None = None,
        price_types: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.supplier = supplier
        self.layout_ = layout
        self.price_types = price_types or []
        self.parsed: SupplierPrice | None = None
        self._titles: list[str] = list(layout.titles) if layout else []
        self._role_boxes: dict[FieldRole, SelectBox] = {}
        self._price_boxes: dict[str, SelectBox] = {}

        self.setWindowTitle(
            "Структура прайса" if layout else f"Прайс поставщика «{supplier.name}»")
        self.setMinimumWidth(660)
        self._build()
        self._fill_from(layout.profile if layout else SupplierProfile())

    # --- интерфейс ------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 4, Metrics.PAD, Metrics.PAD + 4, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(Hint(
            f"Файл разбирается и запоминается за поставщиком «{self.supplier.name}». "
            "Следующий прайс той же структуры будет узнан автоматически — даже "
            "если имя файла ничего о поставщике не говорит.", self))

        self.picker = FilePicker("Файл прайса", "не выбран", self)
        self.picker.file_selected.connect(self._load)
        self.picker.sheet_changed.connect(lambda name: self._reload(name))
        root.addWidget(self.picker)
        root.addWidget(Divider(self))

        root.addWidget(SectionTitle("Что в каких колонках", self))
        root.addWidget(Hint(
            "Роль колонки решает, по чему ищется товар. Автоопределение "
            "ошибается на нестандартных прайсах — здесь его можно поправить.", self))
        self.roles_grid = QGridLayout()
        self.roles_grid.setHorizontalSpacing(Metrics.GAP)
        self.roles_grid.setVerticalSpacing(4)
        self.roles_grid.setColumnStretch(1, 1)
        self.roles_grid.setColumnStretch(3, 1)
        root.addLayout(self.roles_grid)

        root.addWidget(Divider(self))
        root.addWidget(SectionTitle("Какая цена идёт в какой вид цены 1С", self))
        self.price_hint = Hint("", self)
        root.addWidget(self.price_hint)
        self.price_grid = QGridLayout()
        self.price_grid.setHorizontalSpacing(Metrics.GAP)
        self.price_grid.setVerticalSpacing(4)
        self.price_grid.setColumnStretch(1, 1)
        root.addLayout(self.price_grid)

        root.addStretch(1)
        self.status = Hint("", self)
        root.addWidget(self.status)

        self.buttons = QDialogButtonBox(self)
        self.save_button = self.buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        self.save_button.setText("Сохранить структуру")
        self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self._build_roles()
        self._build_prices()
        self._update_ready()

    def _build_roles(self) -> None:
        """Одна строка на роль: у роли одна колонка, обратное соответствие путало бы."""
        _clear(self.roles_grid)
        self._role_boxes = {}
        half = (len(_ROLES) + 1) // 2
        for position, role in enumerate(_ROLES):
            row, column = position % half, (position // half) * 2
            self.roles_grid.addWidget(QLabel(role.title, self), row, column)
            box = SelectBox(self)
            box.setMinimumWidth(190)
            box.addItem(_AUTO, "")
            for title in self._titles:
                box.addItem(title, title)
            self._role_boxes[role] = box
            self.roles_grid.addWidget(box, row, column + 1)

    def _build_prices(self) -> None:
        _clear(self.price_grid)
        self._price_boxes = {}
        if not self.price_types:
            self.price_hint.setText(
                "Виды цен появятся после первого сравнения на вкладке «Быстрая "
                "смена цен» — там они читаются из шаблона выгрузки 1С. Пока их "
                "нет, соответствие подберётся автоматически при сравнении.")
            return
        self.price_hint.setText(
            "Не обязательно: при сравнении соответствие подбирается само. "
            "Заданное здесь будет применено как есть.")
        for row, name in enumerate(self.price_types):
            self.price_grid.addWidget(QLabel(name, self), row, 0)
            box = SelectBox(self)
            box.setMinimumWidth(240)
            box.addItem(_NONE, "")
            for title in self._price_titles():
                box.addItem(title, title)
            self._price_boxes[name] = box
            self.price_grid.addWidget(box, row, 1)

    def _price_titles(self) -> list[str]:
        """Ценовые колонки разобранного файла; для правки — все запомненные."""
        if self.parsed is not None:
            return [column.title for column in self.parsed.price_columns]
        return self._titles

    # --- чтение файла ---------------------------------------------------------

    def _load(self, path: str) -> None:
        self.picker.set_status("чтение файла…", Palette.PRIMARY)
        try:
            sheets = workbook.list_sheets(path)
        except Exception as error:  # noqa: BLE001 — текст уходит в подпись диалога
            self._failed(str(error))
            return
        self.picker.set_sheets(sheets)
        self._reload(None)

    def _reload(self, sheet: str | None) -> None:
        if not self.picker.path:
            return
        self.picker.set_status("разбор листа…", Palette.PRIMARY)
        self.save_button.setEnabled(False)
        run_task(
            pricing.load_supplier,
            self.picker.path,
            sheet,
            on_result=self._parsed,
            on_error=self._failed,
        )

    def _parsed(self, parsed: SupplierPrice) -> None:
        self.parsed = parsed
        self._titles = list(parsed.titles)
        self.picker.set_status(
            f"лист «{parsed.sheet_name}» · {len(parsed.records)} строк · "
            f"ценовых колонок: {len(parsed.price_columns)}", Palette.SUCCESS)
        chosen = self._collect()
        self._build_roles()
        self._build_prices()
        self._fill_from(chosen, detected=parsed)
        self._update_ready()

    def _failed(self, message: str) -> None:
        self.parsed = None
        self.picker.set_status("ошибка чтения", Palette.DANGER)
        self.status.setText(f"Не удалось прочитать файл: {message}")
        self._update_ready()

    # --- перенос значений -----------------------------------------------------

    def _fill_from(self, profile: SupplierProfile, detected: SupplierPrice | None = None) -> None:
        """Ставит сохранённый выбор, а чего нет — то, что распознало автоопределение."""
        auto: dict[FieldRole, str] = {}
        if detected is not None:
            for column in detected.columns:
                auto.setdefault(column.role, column.title)
        for role, box in self._role_boxes.items():
            title = profile.role_map.get(role.value) or auto.get(role, "")
            box.setCurrentIndex(max(box.findData(title), 0))
        for name, box in self._price_boxes.items():
            box.setCurrentIndex(max(box.findData(profile.price_map.get(name, "")), 0))

    def _collect(self) -> SupplierProfile:
        """Собирает выбор пользователя в профиль."""
        profile = SupplierProfile(
            name=self.supplier.name,
            sheet=self.parsed.sheet_name if self.parsed else (
                self.layout_.sheet_name if self.layout_ else ""),
        )
        if self.layout_ is not None:
            profile.separators = self.layout_.profile.separators
            profile.modifier_separators = self.layout_.profile.modifier_separators
        profile.role_map = {
            role.value: box.currentData()
            for role, box in self._role_boxes.items() if box.currentData()
        }
        profile.price_map = {
            name: box.currentData()
            for name, box in self._price_boxes.items() if box.currentData()
        }
        return profile

    def _update_ready(self) -> None:
        ready = self.parsed is not None or self.layout_ is not None
        self.save_button.setEnabled(ready)
        if not ready:
            self.status.setText("Выберите файл прайса поставщика")
        elif self.parsed is None:
            self.status.setText(
                "Файл не выбран — сохранится правка запомненной структуры")
        else:
            self.status.setText("")

    def result_layout(self) -> SupplierLayout:
        """Структура для сохранения в базе."""
        titles = self._titles
        return SupplierLayout(
            id=self.layout_.id if self.layout_ else 0,
            supplier_id=self.supplier.id,
            profile=self._collect(),
            signature=suppliers.signature_of(titles),
            titles=list(titles),
        )

    @property
    def file_name(self) -> str:
        return os.path.basename(self.picker.path) if self.picker.path else ""


def _clear(grid: QGridLayout) -> None:
    while grid.count():
        if widget := grid.takeAt(0).widget():
            widget.deleteLater()
