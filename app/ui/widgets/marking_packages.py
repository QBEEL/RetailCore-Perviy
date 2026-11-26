"""Состав упаковки по свежим сведениям ЧЗ и действия с ней."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QListWidget,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from ...core.marking import reaggregation
from ...core.marking.models import CodeInfo, Contour, Operation, OperationStatus
from ..tasks import run_task
from ..theme import Metrics
from .common import Hint


class PackagesDialog(QDialog):
    journal_changed = Signal()

    def __init__(self, codes: tuple[str, ...], contour: Contour, inn: str,
                 thumbprint: str, parent: QWidget, *, read_only: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Набор или упаковка в ЧЗ")
        self.resize(680, 460)
        self.codes = codes
        self.contour = contour
        self.inn = inn
        self.thumbprint = thumbprint
        self.read_only = read_only
        self._busy = False
        self._linked: dict[str, list[str]] = {}
        self._packages: dict[str, CodeInfo] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        layout.setSpacing(Metrics.GAP)
        self.chooser = QComboBox(self)
        self.chooser.currentIndexChanged.connect(self._show_package)
        layout.addWidget(self.chooser)
        self.details = QPlainTextEdit(self)
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        self.selection = QListWidget(self)
        self.selection.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.selection.itemSelectionChanged.connect(self._sync_removal)
        self.selection.setMaximumHeight(110)
        self.selection.hide()
        layout.addWidget(self.selection)
        self.message = Hint("", self)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.message)

        actions = QHBoxLayout()
        actions.addStretch()
        self.action = QPushButton(self)
        self.action.setAutoDefault(False)
        self.action.clicked.connect(self._submit)
        self.action.hide()
        actions.addWidget(self.action)
        self.close_button = QPushButton("Закрыть", self)
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

    def open(self) -> None:
        self._set_busy(True)
        self.message.setText(f"Получаем актуальный состав из ЧЗ · {self.contour.title}")
        super().open()
        run_task(reaggregation.locate, self.codes, self.inn, self.thumbprint, self.contour,
                 on_result=self._located, on_error=self._lookup_failed)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.chooser.setEnabled(not busy)
        self.selection.setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        if busy:
            self.action.setEnabled(False)

    def reject(self) -> None:
        if not self._busy:
            super().reject()

    def closeEvent(self, event) -> None:
        if self._busy:
            event.ignore()
        else:
            super().closeEvent(event)

    def _located(self, outcome: tuple[dict[str, list[str]], dict[str, CodeInfo], tuple[str, ...]]) -> None:
        self._linked, self._packages, unconfirmed = outcome
        self._set_busy(False)
        for package in self._linked:
            info = self._packages.get(package)
            label = f"{info.product_name} · {package}" if info and info.product_name else package
            self.chooser.addItem(label, package)
        self.chooser.setVisible(len(self._linked) > 1)
        if unconfirmed:
            self.message.setText("ЧЗ не подтвердил сведения о кодах\n" + "\n".join(unconfirmed))
        else:
            self.message.clear()
        if not self._linked and len(unconfirmed) < len(self.codes):
            self.details.setPlainText("По свежим данным ЧЗ у найденных кодов нет родительской упаковки.")

    def _lookup_failed(self, message: str) -> None:
        self._set_busy(False)
        self.chooser.hide()
        self.message.setText(f"Не удалось проверить упаковку в ЧЗ. {message}")

    def _show_package(self) -> None:
        package = self.chooser.currentData()
        if not package:
            return
        info = self._packages.get(package, CodeInfo())
        linked = self._linked.get(package, [])
        self.action.hide()
        self.selection.hide()
        self.selection.clear()

        if not info.found or not info.valid:
            self.details.setPlainText(f"Родительский код {package}\nЧЗ не подтвердил сведения об упаковке."
                                      + "\n\nПроверяемые коды\n" + "\n".join(linked))
            return
        is_set = reaggregation.is_set(info)
        package_type = reaggregation.package_type(info)
        kind = "Набор (КИН)" if is_set else {
            "BOX": "Транспортная упаковка (КИТУ)", "GROUP": "Групповая упаковка (КИГУ)",
        }.get(package_type, package_type or "Упаковка")
        lines = [f"{kind}  {package}", info.product_name,
                 f"Статус  {info.state.title}",
                 f"Владелец  {info.owner_name} {info.owner_inn}".rstrip()]
        if info.parent_code:
            lines.append(f"Вложен в упаковку  {info.parent_code}")
        if info.children:
            lines += ["", f"Вложения по данным ЧЗ ({len(info.children)})"]
            lines.extend(f"{code}  ·  проверяемый код" if code in linked else code for code in info.children)
        else:
            lines += ["", "ЧЗ не вернул список вложений."]
        missing = set(linked) - set(info.children)
        if missing:
            lines += ["", "ЧЗ указал эту упаковку для кодов, но их нет в её составе", *sorted(missing)]

        if self.read_only:
            lines += ["", "Отправка недоступна для текущего черновика. Проверьте ИНН и журнал."]
        elif is_set:
            problem = reaggregation.disband_problem(info, linked, self.inn)
            if problem:
                lines += ["", problem]
            else:
                self.action.setText("Расформировать набор…")
                self.action.setEnabled(True)
                self.action.show()
        elif package_type == "BOX" and not missing and linked:
            self.selection.addItems(linked)
            self.selection.show()
            self.action.setText("Изъять выбранные коды…")
            self.action.setEnabled(False)
            self.action.show()
        self.details.setPlainText("\n".join(lines))

    def _sync_removal(self) -> None:
        if self.selection.isVisible() and not self._busy:
            self.action.setEnabled(bool(self.selection.selectedItems()))

    def _submit(self) -> None:
        if self._busy or self.read_only or self.action.isHidden() or not self.action.isEnabled():
            return
        package = self.chooser.currentData()
        info = self._packages[package]
        if reaggregation.is_set(info):
            task = reaggregation.disband
            codes = tuple(self._linked[package])
            args = (package, codes, info)
            title = "Расформировать набор"
            effect = f"Будут освобождены все вложения ({len(info.children)})."
        else:
            task = reaggregation.send
            codes = tuple(item.text() for item in self.selection.selectedItems())
            args = (package, codes)
            title = "Изъять коды из упаковки"
            effect = f"Выбрано вложений для изъятия: {len(codes)}."

        confirm = QMessageBox(self)
        confirm.setWindowTitle(title)
        confirm.setTextFormat(Qt.TextFormat.PlainText)
        confirm.setText(f"{package}\n{effect}\n\nИНН {self.inn} · {self.contour.title}")
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        confirm.button(QMessageBox.StandardButton.Yes).setText("Подписать и отправить")
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return
        self._set_busy(True)
        self.message.setText("Сверяем состав, подписываем и отправляем документ в ЧЗ…")
        run_task(task, *args, self.inn, self.thumbprint, self.contour,
                 on_result=self._submitted, on_error=self._submit_failed)

    def _submitted(self, operation: Operation) -> None:
        if operation.status is OperationStatus.DONE:
            result = "ЧЗ подтвердил операцию. Повторите проверку кодов для обновления сведений."
        elif operation.status.pending:
            result = "Результат пока неизвестен. Проверьте документ в журнале или кабинете ЧЗ."
        else:
            result = "ЧЗ не подтвердил операцию."
        lines = [result, f"Документ {operation.document_id}" if operation.document_id else "", operation.error]
        self._finish_submission("\n".join(line for line in lines if line))

    def _submit_failed(self, message: str) -> None:
        self._finish_submission(message)

    def _finish_submission(self, message: str) -> None:
        self._set_busy(False)
        self.action.hide()
        self.selection.hide()
        self.chooser.setEnabled(False)
        self.message.setText(message)
        self.journal_changed.emit()
