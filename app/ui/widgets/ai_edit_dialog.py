"""Окно ИИ-правки: просьба → результат на просмотр → «Заменить» или «Отмена».

Окно ничего не меняет в заметке само: оно только получает текст от ИИ. Подставляет
его страница, и только после нажатия «Заменить» — чужой текст не должен занять место
своего без подтверждения.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ...core import ai
from ..tasks import run_task
from ..theme import Metrics, Palette
from .common import Hint

# Просьбы, которые чаще всего нужны: нажатие вписывает текст, его можно доправить.
QUICK_REQUESTS = (
    "Сделай короче",
    "Исправь ошибки",
    "Оформи списком",
    "Перепиши деловым тоном",
)


class AiEditDialog(QDialog):
    def __init__(self, whole: str, selection: str, parent: QWidget | None = None,
                 request: Callable[[str, str], str] | None = None) -> None:
        super().__init__(parent)
        self._whole = whole
        self._selection = selection
        # Запрос подменяется в тестах, чтобы не ходить на сервер.
        self._request = request or ai.edit_text
        self._busy = False
        # Что получилось и к чему относится: страница подставляет это в заметку.
        self.result = ""
        self.for_selection = bool(selection)
        self.setWindowTitle("ИИ-правка")
        self.resize(640, 560)
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(Metrics.GAP)

        scope = QHBoxLayout()
        scope.addWidget(QLabel("Что править:", self))
        self.whole_radio = QRadioButton("Всю страницу", self)
        self.selection_radio = QRadioButton("Выделенное", self)
        group = QButtonGroup(self)
        group.addButton(self.whole_radio)
        group.addButton(self.selection_radio)
        self.selection_radio.setEnabled(bool(self._selection))
        (self.selection_radio if self._selection else self.whole_radio).setChecked(True)
        scope.addWidget(self.whole_radio)
        scope.addWidget(self.selection_radio)
        scope.addStretch(1)
        layout.addLayout(scope)

        quick = QHBoxLayout()
        quick.setSpacing(6)
        self._quick: list[QPushButton] = []
        for text in QUICK_REQUESTS:
            button = QPushButton(text, self)
            button.clicked.connect(lambda _=False, value=text: self._use_quick(value))
            quick.addWidget(button)
            self._quick.append(button)
        quick.addStretch(1)
        layout.addLayout(quick)

        self.instruction = QPlainTextEdit(self)
        self.instruction.setPlaceholderText("Что сделать с текстом, например: «Сделай короче»")
        self.instruction.setFixedHeight(80)
        layout.addWidget(self.instruction)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.preview = QPlainTextEdit(self)
        self.preview.setReadOnly(True)
        self.preview.setVisible(False)
        layout.addWidget(self.preview, 1)

        self.hint = Hint(
            "Текст уйдёт на сервер оплат и дальше в Gemini. Результат вы увидите "
            "до замены и сможете вернуть прежний текст.", self)
        layout.addWidget(self.hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.run_button = QPushButton("Выполнить", self)
        self.run_button.setObjectName("Primary")
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(self._run)
        self.replace_button = QPushButton("Заменить", self)
        self.replace_button.setObjectName("Primary")
        self.replace_button.setVisible(False)
        self.replace_button.clicked.connect(self._accept_result)
        self.cancel_button = QPushButton("Отмена", self)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.replace_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

    # --- действия -------------------------------------------------------------

    def _use_quick(self, text: str) -> None:
        self.instruction.setPlainText(text)
        self.instruction.setFocus()

    def _source(self) -> str:
        return self._selection if self.selection_radio.isChecked() else self._whole

    def _run(self) -> None:
        if self._busy:
            return
        instruction = self.instruction.toPlainText().strip()
        if not instruction:
            self._say("Напишите, что сделать с текстом.", error=True)
            return
        self._set_busy(True)
        self._say("ИИ работает над текстом…")
        self.replace_button.setVisible(False)
        self.preview.setVisible(False)
        self.for_selection = self.selection_radio.isChecked()
        run_task(self._request, instruction, self._source(),
                 on_result=self._done, on_error=self._failed)

    def _done(self, text: object) -> None:
        self._set_busy(False)
        self.result = str(text)
        self.preview.setPlainText(self.result)
        self.preview.setVisible(True)
        self.replace_button.setVisible(True)
        self.run_button.setText("Повторить")
        self._say("Готово. Просмотрите результат и нажмите «Заменить».")

    def _failed(self, message: str) -> None:
        self._set_busy(False)
        self._say(message, error=True)

    def _accept_result(self) -> None:
        if self.result:
            self.accept()

    # --- вид ------------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in (self.run_button, self.instruction, self.whole_radio,
                       self.selection_radio, *self._quick):
            widget.setEnabled(not busy)
        if not busy:
            self.selection_radio.setEnabled(bool(self._selection))

    def _say(self, text: str, *, error: bool = False) -> None:
        color = Palette.DANGER if error else Palette.TEXT_FAINT
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text)
