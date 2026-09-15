"""Диалоги маркировки: под какой организацией входить.

Сертификат по машиночитаемой доверенности действует за нескольких участников
оборота, и система не выбирает за нас: на вход без ИНН боевой контур отвечает
«Невозможно однозначно определить под какой организацией выполняется
авторизация. Укажите запросе INN».

Поэтому выбор спрашивается окном, а сделанный выбор запоминается: набирать ИНН
руками при каждом входе никто не станет. Ввод руками при этом остаётся —
список организаций отдаёт ГИС МТ, и когда она недоступна, вход не должен
упираться в невозможность назвать ИНН.
"""
from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.marking import Organisation
from .. import icons
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle

# Длина ИНН: у организации десять знаков, у предпринимателя двенадцать.
INN_LENGTHS = (10, 12)


class OrganisationDialog(QDialog):
    """Выбор участника оборота, под которым выполняется вход."""

    def __init__(
        self,
        organisations: Sequence[Organisation],
        current_inn: str = "",
        parent: QWidget | None = None,
        note: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Организация")
        self.setMinimumWidth(520)
        self._known = list(organisations)
        self.refresh_wanted = False
        self._build(note)
        self._fill(current_inn)

    # --- разметка ---------------------------------------------------------------

    def _build(self, note: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(SectionTitle("Под какой организацией входить", self))
        root.addWidget(Hint(
            "Сертификат действует за нескольких участников оборота, и система не "
            "выбирает за вас. Выбранная организация запомнится — следующий вход "
            "пройдёт без этого окна.", self))
        if note:
            problem = Hint(note, self)
            problem.setStyleSheet(f"color: {Palette.WARNING};")
            root.addWidget(problem)

        self.list = QListWidget(self)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        self.list.currentRowChanged.connect(self._sync_buttons)
        root.addWidget(self.list, 1)

        manual = QHBoxLayout()
        manual.setSpacing(9)
        self.inn = QLineEdit(self)
        self.inn.setPlaceholderText("ИНН — 10 или 12 цифр")
        self.inn.setMaxLength(12)
        self.inn.setMaximumWidth(180)
        self.name = QLineEdit(self)
        self.name.setPlaceholderText("Название — например, ИП Иванов И. И.")
        self.add_button = QPushButton("Добавить", self)
        self.add_button.setIcon(icons.icon("plus"))
        self.add_button.clicked.connect(self.add_manually)
        self.inn.returnPressed.connect(self.add_manually)
        self.name.returnPressed.connect(self.add_manually)
        manual.addWidget(self.inn)
        manual.addWidget(self.name, 1)
        manual.addWidget(self.add_button)
        root.addLayout(manual)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("Войти")
        self.ok_button.setObjectName("Primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        # Спросить систему заново — обращение к закрытому ключу, поэтому это
        # отдельное решение пользователя, а не то, что окно делает само при
        # каждом открытии.
        refresh = buttons.addButton("Спросить систему",
                                    QDialogButtonBox.ButtonRole.ResetRole)
        refresh.clicked.connect(self._ask_again)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # --- список -----------------------------------------------------------------

    def _fill(self, current_inn: str) -> None:
        self.list.clear()
        for organisation in self._known:
            item = QListWidgetItem(organisation.title, self.list)
            item.setData(Qt.ItemDataRole.UserRole, organisation)
        if current_inn:
            for row, organisation in enumerate(self._known):
                if organisation.inn == current_inn:
                    self.list.setCurrentRow(row)
                    break
        if self.list.currentRow() < 0 and self._known:
            self.list.setCurrentRow(0)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.ok_button.setEnabled(self.list.currentRow() >= 0)

    def add_manually(self) -> None:
        """Добавляет организацию, названную руками.

        Проверяется только длина и то, что это цифры: правильность ИНН выяснит
        сама система, а придумывать здесь свою проверку — значит однажды не
        пустить пользователя с верным номером.
        """
        inn = self.inn.text().strip()
        if not inn.isdigit() or len(inn) not in INN_LENGTHS:
            self.inn.setFocus()
            self.inn.selectAll()
            return
        if any(organisation.inn == inn for organisation in self._known):
            self._select(inn)
            return
        self._known.append(Organisation(inn=inn, name=self.name.text().strip()))
        self.inn.clear()
        self.name.clear()
        self._fill(inn)

    def _select(self, inn: str) -> None:
        for row, organisation in enumerate(self._known):
            if organisation.inn == inn:
                self.list.setCurrentRow(row)
                return

    def _ask_again(self) -> None:
        self.refresh_wanted = True
        self.accept()

    # --- результат ---------------------------------------------------------------

    @property
    def chosen(self) -> Organisation | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    @property
    def organisations(self) -> list[Organisation]:
        """Список вместе с добавленным вручную — его запоминает вкладка."""
        return list(self._known)


class OrderConfirmDialog(QDialog):
    """Подтверждение заказа кодов — единственной операции, которая тратит деньги.

    Спрашивается всегда, а не только в боевом контуре: в песочнице ошибка
    ничего не стоит, но привычка нажимать «Заказать» не глядя вырабатывается
    именно там, а срабатывает потом в бою.

    Показывается ровно то, что уйдёт в запрос: контур, способ выпуска, товары и
    общее количество. Проверять сводку глазами человек может только по ней, а
    не по полям, разбросанным по карточке.
    """

    def __init__(self, request, contour, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Заказ кодов маркировки")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._build(request, contour)

    def _build(self, request, contour) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(SectionTitle(
            f"Заказать {request.total} кодов · {contour.title}"))

        rows = QVBoxLayout()
        rows.setSpacing(4)
        for line in request.lines:
            rows.addWidget(Hint(f"{line.gtin} — {line.quantity} шт."))
        root.addLayout(rows)

        root.addWidget(Hint(f"Способ выпуска: {request.method.title}. "
                            f"Товарная группа: {request.product_group}. "
                            f"Контактное лицо: {request.contact}."))

        warning = Hint(
            "Коды будут выпущены в настоящей системе и оплачены по договору с "
            "ЦРПТ. Отменить заказ нельзя — неиспользованные коды просто "
            "останутся в буфере до конца его срока."
            if contour.value == "production" else
            "Песочница: коды ненастоящие и денег не стоят.")
        if contour.value == "production":
            warning.setStyleSheet(f"color: {Palette.DANGER};")
        root.addWidget(warning)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
        confirm.setText(f"Заказать {request.total}")
        confirm.setIcon(icons.icon("run"))
        # Кнопка подтверждения намеренно не назначена кнопкой по умолчанию:
        # Enter, нажатый по привычке, не должен заказывать коды.
        confirm.setAutoDefault(False)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText("Отмена")
        cancel.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
