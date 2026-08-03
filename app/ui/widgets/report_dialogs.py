"""Диалоги отчётности: формат отчёта и правила объединения магазинов.

Обе настройки общие для отдела, поэтому диалоги показывают, кто и когда правил
запись: «почему в моём отчёте цифры не те» чаще всего объясняется чужой
правкой, а не ошибкой расчёта.
"""
from __future__ import annotations

from typing import Callable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.reports import (
    GROUP_FIELDS,
    Field,
    Metric,
    ReportProfile,
    StoreRule,
    as_fields,
    as_metrics,
)
from ...core.reports.stores import build_map, normalize
from .. import icons
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle


class ProfileDialog(QDialog):
    """Формат отчёта: что в строках, что в колонках, что считаем и что отсекаем."""

    def __init__(self, profile: ReportProfile, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Профиль отчёта")
        self.setMinimumWidth(620)
        self.profile = profile
        self._build()
        self._load()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        form = QFormLayout()
        form.setSpacing(9)
        self.name = QLineEdit(self)
        self.name.setPlaceholderText("SmartBeauty — акции")
        self.supplier = QLineEdit(self)
        self.supplier.setPlaceholderText("Имя поставщика для шапки отчёта")
        form.addRow("Название профиля", self.name)
        form.addRow("Поставщик", self.supplier)
        root.addLayout(form)

        root.addWidget(SectionTitle("Что показать", self))
        root.addWidget(Hint(
            "Строки — то, по чему разбивается отчёт сверху вниз, колонки — разрез "
            "поперёк. По умолчанию это номенклатура и акция: так отчёт собирался "
            "руками.", self))

        picks = QHBoxLayout()
        picks.setSpacing(Metrics.GAP)
        self.rows = _FieldList("Строки", GROUP_FIELDS, self)
        self.columns = _FieldList("Колонки", GROUP_FIELDS, self)
        self.metrics = _MetricList(self)
        for widget in (self.rows, self.columns, self.metrics):
            picks.addWidget(widget, 1)
        root.addLayout(picks)

        root.addWidget(SectionTitle("Что отсечь", self))
        self.promo_only = QCheckBox(
            "Только акционные продажи — строки без акции в отчёт не идут", self)
        self.brands = QLineEdit(self)
        self.brands.setPlaceholderText("BIOREPAIR, BLANX — пусто значит все")
        filters = QFormLayout()
        filters.setSpacing(9)
        filters.addRow("", self.promo_only)
        filters.addRow("Бренды", self.brands)
        root.addLayout(filters)

        root.addWidget(SectionTitle("Оформление файла", self))
        extra = QFormLayout()
        extra.setSpacing(9)
        self.file_name = QLineEdit(self)
        self.file_name.setPlaceholderText("{Месяц} {Год}")
        self.note = QLineEdit(self)
        self.note.setPlaceholderText("Строка примечания под периодом — необязательно")
        self.signatures = QPlainTextEdit(self)
        self.signatures.setPlaceholderText(
            "Подписи под таблицей, по одной в строке")
        self.signatures.setFixedHeight(64)
        self.stores_sheet = QCheckBox(
            "Отдельный лист с итогами по магазинам", self)
        self.apply_rules = QCheckBox(
            "Применять правила объединения магазинов", self)
        extra.addRow("Имя файла", self.file_name)
        extra.addRow("Примечание", self.note)
        extra.addRow("Подписи", self.signatures)
        extra.addRow("", self.stores_sheet)
        extra.addRow("", self.apply_rules)
        root.addLayout(extra)
        root.addWidget(Hint(
            "В имени файла и заголовке подставляются {Месяц}, {Год} и {Поставщик}. "
            "Период берётся из самих данных, а не из даты выгрузки.", self))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=self)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _load(self) -> None:
        profile = self.profile
        self.name.setText(profile.name)
        self.supplier.setText(profile.supplier)
        self.rows.set_checked(profile.rows)
        self.columns.set_checked(profile.columns)
        self.metrics.set_checked(profile.metrics)
        self.promo_only.setChecked(profile.filters.promo_only)
        self.brands.setText(", ".join(profile.filters.brands))
        self.file_name.setText(profile.file_name)
        self.note.setText(profile.note)
        self.signatures.setPlainText("\n".join(profile.signatures))
        self.stores_sheet.setChecked(profile.stores_sheet)
        self.apply_rules.setChecked(profile.apply_store_rules)

    def _accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "Профиль отчёта",
                                "У профиля должно быть название.")
            return
        rows = self.rows.checked()
        if not rows:
            QMessageBox.warning(
                self, "Профиль отчёта",
                "Выберите хотя бы одно поле для строк — иначе отчёт не из чего строить.")
            return
        if not self.metrics.checked():
            QMessageBox.warning(
                self, "Профиль отчёта",
                "Выберите хотя бы один показатель.")
            return
        if overlap := set(rows) & set(self.columns.checked()):
            QMessageBox.warning(
                self, "Профиль отчёта",
                "Одно и то же поле нельзя поставить и в строки, и в колонки: "
                + ", ".join(role.title for role in overlap))
            return
        self._apply()
        self.accept()

    def _apply(self) -> None:
        profile = self.profile
        profile.name = self.name.text().strip()
        profile.supplier = self.supplier.text().strip()
        profile.rows = self.rows.checked()
        profile.columns = self.columns.checked()
        profile.metrics = self.metrics.checked()
        profile.filters.promo_only = self.promo_only.isChecked()
        profile.filters.brands = _split(self.brands.text())
        profile.file_name = self.file_name.text().strip() or "{Месяц} {Год}"
        profile.note = self.note.text().strip()
        profile.signatures = [line.strip() for line
                              in self.signatures.toPlainText().splitlines()
                              if line.strip()]
        profile.stores_sheet = self.stores_sheet.isChecked()
        profile.apply_store_rules = self.apply_rules.isChecked()


class _CheckList(QWidget):
    """Список с галочками. Порядок отметок задаёт порядок в отчёте.

    В данных элемента хранится строковое значение перечисления, а не сам его
    член. `Field` и `Metric` наследуют `str`, поэтому Qt кладёт их в QVariant
    как обычную строку и такой же строкой возвращает — список внешне остаётся
    прежним, а `role.value` дальше по коду падает. Раз обратно всё равно
    приходит строка, честнее её и класть, а разбирать на выходе.
    """

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(QLabel(title, self))
        self.list = QListWidget(self)
        self.list.setFixedHeight(150)
        layout.addWidget(self.list)

    def _fill(self, options) -> None:
        for option in options:
            item = QListWidgetItem(option.title, self.list)
            item.setData(Qt.ItemDataRole.UserRole, option.value)
            item.setCheckState(Qt.CheckState.Unchecked)

    def set_checked(self, values: Sequence[object]) -> None:
        chosen = {str(getattr(value, "value", value)) for value in values}
        for index in range(self.list.count()):
            item = self.list.item(index)
            item.setCheckState(
                Qt.CheckState.Checked
                if str(item.data(Qt.ItemDataRole.UserRole)) in chosen
                else Qt.CheckState.Unchecked)

    def _raw(self) -> list[str]:
        return [str(self.list.item(i).data(Qt.ItemDataRole.UserRole))
                for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]


class _FieldList(_CheckList):
    def __init__(self, title: str, fields: Sequence[Field],
                 parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self._fill(fields)

    def checked(self) -> list[Field]:
        return as_fields(self._raw())


class _MetricList(_CheckList):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Показатели", parent)
        self._fill(Metric)

    def checked(self) -> list[Metric]:
        return as_metrics(self._raw())


class StoreRulesDialog(QDialog):
    """Правила «магазин-источник → магазин-приёмник».

    Цикл проверяется сразу при правке, а не при сборке отчёта: с замкнутой
    цепочкой переносы не применяются вовсе, и узнавать об этом за минуту до
    отправки файла поставщику — худший момент из возможных.
    """

    def __init__(
        self,
        rules: Sequence[StoreRule],
        stores: Sequence[str],
        save: Callable[[StoreRule], StoreRule],
        delete: Callable[[int], None],
        parent: QWidget | None = None,
        *,
        may_delete: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Объединение магазинов")
        self.setMinimumSize(760, 520)
        self._rules = list(rules)
        self._stores = list(stores)
        self._save = save
        self._delete = delete
        self._may_delete = may_delete
        self._build()
        self._refresh()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(SectionTitle("Правила объединения магазинов", self))
        root.addWidget(Hint(
            "Продажи магазина-источника учитываются за магазином-приёмником. "
            "Правила общие для всех менеджеров и применяются по цепочке: если "
            "A → B и B → C, продажи A попадут в C.", self))

        editor = QHBoxLayout()
        editor.setSpacing(9)
        self.source = QComboBox(self)
        self.source.setEditable(True)
        self.source.addItems(self._stores)
        self.source.setCurrentText("")
        self.source.lineEdit().setPlaceholderText("Магазин-источник")
        self.target = QComboBox(self)
        self.target.setEditable(True)
        self.target.addItems(self._stores)
        self.target.setCurrentText("")
        self.target.lineEdit().setPlaceholderText("Магазин-приёмник")
        self.comment = QLineEdit(self)
        self.comment.setPlaceholderText("Зачем — необязательно")
        add = QPushButton("Добавить", self)
        add.setIcon(icons.icon("plus"))
        add.clicked.connect(self._add)
        editor.addWidget(self.source, 3)
        editor.addWidget(QLabel("→", self))
        editor.addWidget(self.target, 3)
        editor.addWidget(self.comment, 3)
        editor.addWidget(add)
        root.addLayout(editor)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(
            ["Источник", "Приёмник", "Комментарий", "Изменил", "Активно"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)

        self.problem = Hint("", self)
        self.problem.setStyleSheet(f"color: {Palette.DANGER};")
        root.addWidget(self.problem)

        actions = QHBoxLayout()
        actions.setSpacing(9)
        self.toggle_button = QPushButton("Включить / отключить", self)
        self.toggle_button.clicked.connect(self._toggle)
        self.delete_button = QPushButton("Удалить", self)
        self.delete_button.setObjectName("Danger")
        self.delete_button.setIcon(icons.icon("trash"))
        self.delete_button.clicked.connect(self._remove)
        self.delete_button.setEnabled(self._may_delete)
        if not self._may_delete:
            self.delete_button.setToolTip(
                "Удалять общие правила может только администратор")
        actions.addWidget(self.toggle_button)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        close = QPushButton("Закрыть", self)
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        root.addLayout(actions)

    # --- данные ---------------------------------------------------------------

    def _refresh(self) -> None:
        self.table.setRowCount(len(self._rules))
        for row, rule in enumerate(self._rules):
            moment = f"{rule.updated_at:%d.%m.%y}" if rule.updated_at else ""
            who = " · ".join(part for part in (rule.updated_by, moment) if part)
            for column, text in enumerate((rule.source, rule.target, rule.comment,
                                           who, "да" if rule.enabled else "нет")):
                item = QTableWidgetItem(text)
                if not rule.enabled:
                    item.setForeground(Qt.GlobalColor.gray)
                self.table.setItem(row, column, item)
        self._check_cycles()

    def _check_cycles(self) -> None:
        mapping = build_map(self._rules)
        if mapping.cycles:
            loops = "; ".join(" → ".join(loop) for loop in mapping.cycles)
            self.problem.setText(
                f"Цепочка замкнута в кольцо: {loops}. Пока это так, переносы "
                "не применяются ни к одному магазину.")
        else:
            self.problem.setText("")

    def _current(self) -> StoreRule | None:
        row = self.table.currentRow()
        return self._rules[row] if 0 <= row < len(self._rules) else None

    # --- действия -------------------------------------------------------------

    def _add(self) -> None:
        source = self.source.currentText().strip()
        target = self.target.currentText().strip()
        if not source or not target:
            QMessageBox.warning(self, "Объединение магазинов",
                                "Укажите и источник, и приёмник.")
            return
        if normalize(source) == normalize(target):
            QMessageBox.warning(self, "Объединение магазинов",
                                "Магазин нельзя объединить сам с собой.")
            return
        candidate = StoreRule(source=source, target=target,
                              comment=self.comment.text().strip())
        if loops := build_map([*self._rules, candidate]).cycles:
            QMessageBox.warning(
                self, "Объединение магазинов",
                "Такое правило замкнёт цепочку в кольцо:\n"
                + "\n".join(" → ".join(loop) for loop in loops))
            return
        self._store(candidate)
        self.source.setCurrentText("")
        self.target.setCurrentText("")
        self.comment.clear()

    def _toggle(self) -> None:
        if (rule := self._current()) is None:
            return
        rule.enabled = not rule.enabled
        self._store(rule)

    def _remove(self) -> None:
        if (rule := self._current()) is None:
            return
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Удалить правило")
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setText(f"Удалить правило «{rule.source} → {rule.target}»?")
        confirm.setInformativeText(
            "Правило общее: отчёты всех менеджеров начнут считать этот магазин "
            "отдельно.")
        yes = confirm.addButton("Удалить", QMessageBox.ButtonRole.DestructiveRole)
        confirm.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        confirm.exec()
        if confirm.clickedButton() is not yes:
            return
        try:
            self._delete(rule.id)
        except Exception as error:  # noqa: BLE001 — текст показываем пользователю
            QMessageBox.critical(self, "Объединение магазинов", str(error))
            return
        self._rules = [item for item in self._rules if item.id != rule.id]
        self._refresh()

    def _store(self, rule: StoreRule) -> None:
        try:
            saved = self._save(rule)
        except Exception as error:  # noqa: BLE001 — текст показываем пользователю
            QMessageBox.critical(self, "Объединение магазинов", str(error))
            return
        for index, existing in enumerate(self._rules):
            if normalize(existing.source) == normalize(saved.source):
                self._rules[index] = saved
                break
        else:
            self._rules.append(saved)
        self._rules.sort(key=lambda item: normalize(item.source))
        self._refresh()

    @property
    def rules(self) -> list[StoreRule]:
        return list(self._rules)


def _split(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]
