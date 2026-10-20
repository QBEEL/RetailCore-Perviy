"""Окно ИИ-правки: просьба → результат на просмотр → «Заменить» или «Отмена».

Окно ничего не меняет в заметке само: оно только получает текст от ИИ. Подставляет
его страница, и только после нажатия «Заменить» — чужой текст не должен занять место
своего без подтверждения.

Сверху две полосы: сколько обращений осталось на сегодня и насколько велик правимый
текст. Обе нужны до нажатия «Выполнить»: человек видит, хватит ли ему лимита и
поместится ли текст в один запрос, а не узнаёт об этом из отказа.
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
from .usage_meter import UsageMeter

# Просьбы, которые чаще всего нужны: нажатие вписывает текст, его можно доправить.
QUICK_REQUESTS = (
    "Сделай короче",
    "Исправь ошибки",
    "Оформи списком",
    "Перепиши деловым тоном",
)


class AiEditDialog(QDialog):
    def __init__(self, whole: str, selection: str, parent: QWidget | None = None,
                 request: Callable[[str, str], str] | None = None,
                 usage: ai.Usage | None = None,
                 load_usage: Callable[[], ai.Usage] | None = None) -> None:
        super().__init__(parent)
        self._whole = whole
        self._selection = selection
        # Запрос и чтение остатка подменяются в тестах, чтобы не ходить на сервер.
        self._request = request or ai.edit_text
        self._load_usage = load_usage or ai.usage
        self._usage = usage
        self._busy = False
        # Последняя причина, по которой «Выполнить» закрыто, — её текст стоит в строке
        # состояния, и снять его нужно, только когда причина исчезнет.
        self._reason_shown = ""
        # Что получилось и к чему относится: страница подставляет это в заметку.
        self.result = ""
        self.for_selection = bool(selection)
        self.setWindowTitle("ИИ-правка")
        self.resize(660, 600)
        self._build()
        self._refresh_size()
        if usage is not None:
            self._show_usage(usage)
        self._refresh_usage()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(Metrics.GAP)

        meters = QHBoxLayout()
        meters.setSpacing(Metrics.PAD * 2)
        self.usage_meter = UsageMeter("Обращения к ИИ сегодня", self)
        self.size_meter = UsageMeter("Размер текста", self)
        meters.addWidget(self.usage_meter, 1)
        meters.addWidget(self.size_meter, 1)
        layout.addLayout(meters)

        scope = QHBoxLayout()
        scope.addWidget(QLabel("Что править:", self))
        self.whole_radio = QRadioButton("Всю страницу", self)
        self.selection_radio = QRadioButton("Выделенное", self)
        group = QButtonGroup(self)
        group.addButton(self.whole_radio)
        group.addButton(self.selection_radio)
        self.selection_radio.setEnabled(bool(self._selection))
        (self.selection_radio if self._selection else self.whole_radio).setChecked(True)
        self.whole_radio.toggled.connect(lambda _: self._on_scope_changed())
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
        self.instruction.textChanged.connect(self._limit_instruction)
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

    # --- пределы --------------------------------------------------------------

    def _max_text(self) -> int:
        return self._usage.max_text if self._usage else ai.MAX_TEXT

    def _max_instruction(self) -> int:
        return self._usage.max_instruction if self._usage else ai.MAX_INSTRUCTION

    def _refresh_usage(self) -> None:
        run_task(self._load_usage, on_result=self._usage_loaded, on_error=self._usage_failed)

    def _usage_loaded(self, usage: object) -> None:
        if not isinstance(usage, ai.Usage):
            return
        self._usage = usage
        self._show_usage(usage)
        self._refresh_size()

    def _show_usage(self, usage: ai.Usage) -> None:
        self.usage_meter.show_usage(usage)
        self._sync_run()

    def _usage_failed(self, message: str) -> None:
        # Остаток не удалось прочитать: показываем это словом, а решение остаётся за
        # сервером — он откажет сам, если предел исчерпан.
        self.usage_meter.show_message("нет данных", message)

    def _refresh_size(self) -> None:
        text = self._source()
        what = "Вся страница. " if self.whole_radio.isChecked() else "Выделенный фрагмент. "
        self.size_meter.show_size(len(text.strip()), self._max_text(), what)
        self._sync_run()

    def _on_scope_changed(self) -> None:
        self._refresh_size()

    def _limit_instruction(self) -> None:
        limit = self._max_instruction()
        text = self.instruction.toPlainText()
        if len(text) > limit:
            self.instruction.setPlainText(text[:limit])
            cursor = self.instruction.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.instruction.setTextCursor(cursor)

    def _blocked(self) -> str:
        """Почему «Выполнить» недоступно; пусто — можно."""
        usage = self._usage
        if usage is not None and not usage.configured:
            return "ИИ на сервере не настроен. Сообщите администратору."
        if usage is not None and usage.closed:
            return "Администратор закрыл вам ИИ-правку."
        if usage is not None and usage.remaining == 0:
            return f"Лимит на сегодня исчерпан. Счётчик обнулится {ai.reset_in(usage.reset_seconds)}."
        if len(self._source().strip()) > self._max_text():
            return "Текст слишком длинный для одного запроса: выделите часть."
        return ""

    def _sync_run(self) -> None:
        reason = self._blocked()
        self.run_button.setEnabled(not self._busy and not reason)
        if self._busy or self.result:
            return
        if reason:
            self._say(reason, error=True)
            self._reason_shown = reason
        elif self._reason_shown and self.status.text() == self._reason_shown:
            # Причина отпала (например, выбрали фрагмент покороче) — убираем только
            # её, а не чужое сообщение об ошибке запроса.
            self._say("")
            self._reason_shown = ""

    # --- действия -------------------------------------------------------------

    def _use_quick(self, text: str) -> None:
        self.instruction.setPlainText(text)
        self.instruction.setFocus()

    def _source(self) -> str:
        return self._selection if self.selection_radio.isChecked() else self._whole

    def _run(self) -> None:
        if self._busy or self._blocked():
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
        # Обращение потрачено: полоса должна показать новый остаток.
        self._refresh_usage()

    def _failed(self, message: str) -> None:
        self._set_busy(False)
        self._say(message, error=True)
        # Отказ мог быть как раз из-за предела — остаток надо перечитать.
        self._refresh_usage()

    def _accept_result(self) -> None:
        if self.result:
            self.accept()

    # --- вид ------------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in (self.instruction, self.whole_radio, self.selection_radio, *self._quick):
            widget.setEnabled(not busy)
        if not busy:
            self.selection_radio.setEnabled(bool(self._selection))
        self._sync_run()

    def _say(self, text: str, *, error: bool = False) -> None:
        color = Palette.DANGER if error else Palette.TEXT_FAINT
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text)
