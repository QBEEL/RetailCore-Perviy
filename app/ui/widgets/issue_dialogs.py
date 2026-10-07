"""Диалоги получения, печати и ввода в оборот кодов маркировки.

Три шага, и на каждом человек подтверждает то, что нельзя вернуть:

- **получение** — коды выдаются из буфера безвозвратно, поэтому спрашивается
  количество и показывается, куда они будут сохранены;
- **печать** — этикетка видна на экране до того, как уйдёт на принтер, а
  пробная печатается образцом, а не настоящим кодом;
- **ввод в оборот** — файл готовится по уже напечатанным кодам, и перед
  сохранением видно, сколько их и под каким ИНН.

Кнопки подтверждения не назначены кнопками по умолчанию: Enter, нажатый по
привычке, не должен забирать коды из буфера.
"""
from __future__ import annotations

import os
from dataclasses import replace
from typing import Sequence

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.marking import introduce, issue, labels
from ...core.marking.labels import LabelSpec
from .. import icons, label_print
from ..theme import Metrics, Palette
from .common import Hint, SectionTitle


def _buttons(parent: QDialog, ok_text: str, ok_icon: str,
             cancel_text: str = "Отмена") -> QDialogButtonBox:
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                           | QDialogButtonBox.StandardButton.Cancel)
    confirm = box.button(QDialogButtonBox.StandardButton.Ok)
    confirm.setText(ok_text)
    confirm.setIcon(icons.icon(ok_icon))
    confirm.setAutoDefault(False)
    cancel = box.button(QDialogButtonBox.StandardButton.Cancel)
    cancel.setText(cancel_text)
    cancel.setDefault(True)
    box.accepted.connect(parent.accept)
    box.rejected.connect(parent.reject)
    return box


class IssueDialog(QDialog):
    """Какие коды и сколько забрать из буфера заказа."""

    def __init__(self, order, products: dict[str, issue.Product], contour,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Получить коды из заказа")
        self.setModal(True)
        self.setMinimumWidth(560)
        self._order = order
        self._products = products
        self._contour = contour
        self._buffers = [item for item in order.buffers if item.left > 0]
        self._build()
        self._on_buffer_changed()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle(f"Заказ {self._order.id}"))

        form = QFormLayout()
        form.setSpacing(9)
        self.buffer_box = QComboBox(self)
        for buffer in self._buffers:
            product = self._products.get(buffer.gtin)
            name = f" · {product.name}" if product and product.name else ""
            self.buffer_box.addItem(
                f"{buffer.gtin}{name} — доступно {buffer.left}", buffer.gtin)
        self.buffer_box.currentIndexChanged.connect(self._on_buffer_changed)
        form.addRow("Товар", self.buffer_box)

        self.quantity = QSpinBox(self)
        self.quantity.setRange(1, 1)
        form.addRow("Сколько кодов", self.quantity)

        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("Печатается на этикетке справа от кода")
        form.addRow("Название на этикетке", self.name_edit)

        self.tnved_edit = QLineEdit(self)
        self.tnved_edit.setPlaceholderText("Нужен для файла ввода в оборот")
        self.tnved_edit.setMaxLength(10)
        form.addRow("ТН ВЭД", self.tnved_edit)
        root.addLayout(form)

        warning = Hint(
            "Коды выдаются из буфера безвозвратно: повторно тот же блок "
            "стандартным методом не выдаётся. Поэтому они сразу записываются "
            f"на диск в {issue.folder()} и лежат там, пока вы их не удалите.")
        warning.setStyleSheet(f"color: {Palette.WARNING};")
        root.addWidget(warning)
        self.confirm_box = _buttons(self, "Получить коды", "download")
        root.addWidget(self.confirm_box)

    def _current_buffer(self):
        index = self.buffer_box.currentIndex()
        return self._buffers[index] if 0 <= index < len(self._buffers) else None

    def _on_buffer_changed(self) -> None:
        buffer = self._current_buffer()
        ok = self.confirm_box.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(buffer is not None)
        if buffer is None:
            return
        self.quantity.setRange(1, min(buffer.left, issue.MAX_PER_REQUEST))
        self.quantity.setValue(min(buffer.left, issue.MAX_PER_REQUEST))
        product = self._products.get(buffer.gtin)
        self.name_edit.setText(product.name if product else "")
        self.tnved_edit.setText(product.tnved if product else "")

    @property
    def gtin(self) -> str:
        buffer = self._current_buffer()
        return buffer.gtin if buffer else ""

    @property
    def count(self) -> int:
        return self.quantity.value()

    @property
    def product(self) -> issue.Product:
        return issue.Product(gtin=self.gtin, name=self.name_edit.text().strip(),
                             tnved=self.tnved_edit.text().strip())


class PrintDialog(QDialog):
    """Печать блока кодов на принтер этикеток."""

    def __init__(self, batch: issue.Batch, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Печать этикеток")
        self.setModal(True)
        self.setMinimumWidth(620)
        self.batch = batch
        self._settings = labels.load()
        self._spec = self._settings.spec()
        # Сколько этикеток напечатано за время жизни окна — вызывающий
        # обновляет по нему строку блока.
        self.printed_now = 0
        self._build()
        self._refresh()

    # --- разметка -----------------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle(self.batch.title))

        form = QFormLayout()
        form.setSpacing(9)
        self.printer_box = QComboBox(self)
        names = label_print.printers()
        self.printer_box.addItems(names)
        wanted = self._settings.printer or label_print.suggested_printer()
        if wanted in names:
            self.printer_box.setCurrentText(wanted)
        form.addRow("Принтер", self.printer_box)

        self.pdf_box = QCheckBox("Сохранить в PDF вместо печати", self)
        form.addRow("", self.pdf_box)

        row = QHBoxLayout()
        row.setSpacing(9)
        self.width = self._spin(self._spec.width, 10, 200, 1, " мм")
        self.height = self._spin(self._spec.height, 10, 200, 1, " мм")
        row.addWidget(QLabel("Этикетка"))
        row.addWidget(self.width)
        row.addWidget(QLabel("×"))
        row.addWidget(self.height)
        row.addWidget(QLabel("Символ"))
        self.dm_size = self._spin(self._spec.dm_size, 8, 40, 1, " мм")
        row.addWidget(self.dm_size)
        row.addStretch(1)
        form.addRow("Размер", row)

        shift = QHBoxLayout()
        shift.setSpacing(9)
        self.offset_x = self._spin(self._spec.offset_x, -10, 10, 1, " мм", step=0.5)
        self.offset_y = self._spin(self._spec.offset_y, -10, 10, 1, " мм", step=0.5)
        shift.addWidget(QLabel("вправо"))
        shift.addWidget(self.offset_x)
        shift.addWidget(QLabel("вниз"))
        shift.addWidget(self.offset_y)
        shift.addWidget(Hint("Если печать съезжает, подвиньте на долю миллиметра"))
        shift.addStretch(1)
        form.addRow("Сдвиг", shift)

        text = QHBoxLayout()
        text.setSpacing(9)
        self.show_name = QCheckBox("Название", self)
        self.show_name.setChecked(self._spec.show_name)
        self.show_text = QCheckBox("GTIN и серийный номер", self)
        self.show_text.setChecked(self._spec.show_text)
        self.name_font = self._spin(self._spec.name_font, 3, 20, 1, " пт", step=0.5)
        text.addWidget(self.show_name)
        text.addWidget(self.show_text)
        text.addWidget(QLabel("шрифт"))
        text.addWidget(self.name_font)
        text.addStretch(1)
        form.addRow("Текст", text)

        rng = QHBoxLayout()
        rng.setSpacing(9)
        self.start = QSpinBox(self)
        self.start.setRange(1, max(1, self.batch.total))
        self.start.setValue(min(self.batch.printed + 1, max(1, self.batch.total)))
        self.count = QSpinBox(self)
        self.count.setRange(1, max(1, self.batch.total))
        self.count.setValue(max(1, self.batch.left))
        rng.addWidget(QLabel("с №"))
        rng.addWidget(self.start)
        rng.addWidget(QLabel("штук"))
        rng.addWidget(self.count)
        self.range_hint = Hint("")
        rng.addWidget(self.range_hint, 1)
        form.addRow("Что печатать", rng)
        root.addLayout(form)

        self.picture = QLabel(self)
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.picture)
        self.hint = Hint("")
        root.addWidget(self.hint)

        row = QHBoxLayout()
        row.setSpacing(9)
        self.test_button = QPushButton("Пробная этикетка", self)
        self.test_button.setIcon(icons.icon("run"))
        self.test_button.setToolTip(
            "Печатает образец, а не настоящий код: коды выдаются безвозвратно, "
            "и тратить их на проверку макета нельзя")
        self.test_button.setAutoDefault(False)
        self.test_button.clicked.connect(self._print_test)
        row.addWidget(self.test_button)
        row.addStretch(1)
        self.print_button = QPushButton("Печатать", self)
        self.print_button.setObjectName("Primary")
        self.print_button.setIcon(icons.icon("run"))
        self.print_button.setAutoDefault(False)
        self.print_button.clicked.connect(self._print)
        row.addWidget(self.print_button)
        close = QPushButton("Закрыть", self)
        close.setDefault(True)
        close.clicked.connect(self.accept)
        row.addWidget(close)
        root.addLayout(row)

        for control in (self.width, self.height, self.dm_size, self.offset_x,
                        self.offset_y, self.name_font):
            control.valueChanged.connect(self._refresh)
        for box in (self.show_name, self.show_text, self.pdf_box):
            box.toggled.connect(self._refresh)
        self.start.valueChanged.connect(self._refresh)
        self.count.valueChanged.connect(self._refresh)

    def _spin(self, value: float, low: float, high: float, decimals: int,
              suffix: str, step: float = 1.0) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setRange(low, high)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setSuffix(suffix)
        spin.setValue(value)
        return spin

    # --- состояние ----------------------------------------------------------------

    def _spec_now(self) -> LabelSpec:
        spec = replace(self._spec)
        spec.width = self.width.value()
        spec.height = self.height.value()
        spec.dm_size = self.dm_size.value()
        spec.offset_x = self.offset_x.value()
        spec.offset_y = self.offset_y.value()
        spec.show_name = self.show_name.isChecked()
        spec.show_text = self.show_text.isChecked()
        spec.name_font = self.name_font.value()
        spec.text_font = self.name_font.value()
        return spec

    def _selected(self) -> list[str]:
        first = self.start.value() - 1
        return self.batch.codes[first:first + self.count.value()]

    def _refresh(self) -> None:
        spec = self._spec_now()
        selected = self._selected()
        sample = selected[0] if selected else label_print.SAMPLE_CODE
        pixmap = QPixmap.fromImage(
            label_print.preview(spec, sample, self.batch.name or "Название товара",
                                zoom=3))
        self.picture.setPixmap(pixmap)
        # Высота по картинке: иначе при смене размера этикетки её обрезает.
        self.picture.setFixedHeight(pixmap.height() + 8)
        names = label_print.printers()
        pdf = self.pdf_box.isChecked()
        self.printer_box.setEnabled(not pdf)
        problems = list(spec.problems)
        if not pdf and not names:
            problems.append("в Windows не найден ни один принтер")
        left = self.batch.total - (self.start.value() - 1)
        self.count.blockSignals(True)
        self.count.setMaximum(max(1, left))
        self.count.blockSignals(False)
        self.range_hint.setText(
            f"выбрано {len(selected)}, уже напечатано {self.batch.printed} "
            f"из {self.batch.total}")
        self.print_button.setText(f"Печатать {len(selected)}"
                                  if not pdf else f"В PDF {len(selected)}")
        self.print_button.setEnabled(bool(selected) and not problems)
        self.test_button.setEnabled(not problems)
        if problems:
            self.hint.setStyleSheet(f"color: {Palette.WARNING};")
            self.hint.setText("Печать недоступна: " + "; ".join(problems) + ".")
        else:
            self.hint.setStyleSheet("")
            self.hint.setText(
                "Так этикетка выйдет на принтере 203 dpi. Символ читается "
                "сканером при размере не меньше 3 точек на модуль; пробная "
                "этикетка печатается образцом и кодов не тратит.")

    def _remember(self) -> None:
        self._settings.printer = self.printer_box.currentText()
        self._settings.label = self._spec_now()
        try:
            labels.save(self._settings)
        except OSError:
            pass

    # --- печать -------------------------------------------------------------------

    def _target(self) -> tuple[str, str] | None:
        """Принтер и путь PDF; `None` — человек передумал сохранять в PDF."""
        if not self.pdf_box.isChecked():
            return self.printer_box.currentText(), ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить этикетки в PDF",
            os.path.join(os.path.expanduser("~"), f"Этикетки {self.batch.gtin}.pdf"),
            "PDF (*.pdf)")
        return ("", path) if path else None

    def _print_test(self) -> None:
        target = self._target()
        if target is None:
            return
        try:
            label_print.print_codes([label_print.SAMPLE_CODE],
                                    self.batch.name or "Название товара",
                                    self._spec_now(), target[0], pdf_path=target[1])
        except label_print.PrintError as error:
            self._fail(str(error))
            return
        self._remember()
        self.hint.setStyleSheet("")
        self.hint.setText("Пробная этикетка отправлена. Проверьте положение символа "
                          "и прочитайте его сканером.")

    def _print(self) -> None:
        selected = self._selected()
        target = self._target()
        if target is None or not selected:
            return
        first = self.start.value() - 1
        progress = QProgressDialog("Печатаем этикетки…", "Остановить", 0,
                                   len(selected), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        def step(done: int, total: int) -> bool:
            progress.setValue(done)
            # Прогресс запоминается порциями: запись на диск после каждой
            # этикетки замедлила бы печать, а потерять при сбое хочется
            # немногое.
            if done % 25 == 0:
                issue.mark_printed(self.batch, first + done)
            QApplication.processEvents()
            return not progress.wasCanceled()

        done = 0
        try:
            done = label_print.print_codes(
                selected, self.batch.name, self._spec_now(), target[0],
                pdf_path=target[1], progress=step)
        except label_print.PrintError as error:
            self._fail(str(error))
        finally:
            progress.close()
            if done:
                issue.mark_printed(self.batch, first + done)
                self.printed_now += done
        if done:
            self._remember()
            self.start.setMaximum(max(1, self.batch.total))
            self.start.setValue(min(self.batch.printed + 1, self.batch.total))
            self.count.setValue(max(1, self.batch.left))
            self._refresh()
            self.hint.setStyleSheet("")
            self.hint.setText(
                f"Отправлено на печать: {done}. Напечатано всего "
                f"{self.batch.printed} из {self.batch.total}."
                + ("" if self.batch.left else " Блок напечатан целиком."))

    def _fail(self, message: str) -> None:
        self.hint.setStyleSheet(f"color: {Palette.DANGER};")
        self.hint.setText(message)


class IntroduceDialog(QDialog):
    """Ввод в оборот: отправить документ из программы или сохранить файл.

    Документа два, и это не прихоть: от способа выпуска кодов зависит, какой нужен.
    Коды, заказанные способом «Перемаркировка», вводятся документом
    «Перемаркировка» (причина, например `KM_SPOILED`), а коды остатков — «Ввод в
    оборот. Маркировка остатков». Какой подаёт это рабочее место, запоминается.

    Отправка — действие юридически значимое и необратимое, поэтому кроме самого
    окна, где видно, сколько кодов и под каким ИНН уйдёт, спрашивается ещё одно
    подтверждение с названием контура. Кнопки подтверждения не назначены кнопками
    по умолчанию: Enter, нажатый по привычке, ничего не отправляет.
    """

    SCOPES = ("pending", "printed", "all")

    def __init__(self, batch: issue.Batch, inn: str, contour,
                 send_problem: "str | dict[str, str]" = "",
                 parent: QWidget | None = None, *,
                 previous: Sequence[str] = (),
                 fixed_range: tuple[int, int] | None = None) -> None:
        """`previous` и `fixed_range` — для замены одного кода из проверки.

        `fixed_range` — какие коды блока вводятся, [от, до): вместо выбора
        «напечатанные/все» берётся ровно он, а документ остаётся
        перемаркировкой. `previous` — старые КИ, идущие в том же порядке.
        """
        super().__init__(parent)
        self.setWindowTitle("Ввод в оборот")
        self.setModal(True)
        self.setMinimumWidth(640)
        self.batch = batch
        self._fixed_range = fixed_range
        self._previous = tuple(previous)
        self._contour = contour
        # Почему отправить нельзя: не выполнен вход, не тот контур, группа без
        # поддержки. Пусто — можно. Файл сохранить можно всегда. Причины разные
        # для двух видов документа, поэтому приходят словарём.
        if isinstance(send_problem, str):
            send_problem = {"remark": send_problem, "ostatky": send_problem}
        self._send_problems = send_problem
        self._settings = labels.load()
        self.action = ""
        self.saved_path = ""
        self.covered = 0
        self._build(inn)
        self._on_kind_changed()

    # --- разметка ------------------------------------------------------------------

    def _build(self, inn: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD, Metrics.PAD, Metrics.PAD, Metrics.PAD)
        root.setSpacing(Metrics.GAP)
        root.addWidget(SectionTitle("Ввод в оборот"))
        root.addWidget(Hint(self.batch.title))

        form = QFormLayout()
        form.setSpacing(9)
        self.kind_box = QComboBox(self)
        self.kind_box.addItem("Перемаркировка (коды заказаны способом «Перемаркировка»)",
                              "remark")
        self.kind_box.addItem("Маркировка остатков", "ostatky")
        index = self.kind_box.findData(self._settings.introduce_kind)
        self.kind_box.setCurrentIndex(max(0, index))
        form.addRow("Документ", self.kind_box)

        self.inn_edit = QLineEdit(inn, self)
        self.inn_edit.setPlaceholderText("ИНН владельца кодов")
        self.inn_edit.setMaxLength(12)
        form.addRow("ИНН участника", self.inn_edit)
        self.tnved_edit = QLineEdit(self.batch.tnved, self)
        self.tnved_edit.setMaxLength(10)
        self.tnved_edit.setPlaceholderText("10 цифр")
        form.addRow("ТН ВЭД", self.tnved_edit)

        self.scope_box = QComboBox(self)
        printed = min(self.batch.printed, self.batch.total)
        sent = min(self.batch.introduced_count, self.batch.total)
        waiting = max(0, printed - sent)
        self.scope_box.addItem(f"Напечатанные, ещё не отправленные — {waiting}", "pending")
        self.scope_box.addItem(f"Все напечатанные — {printed}", "printed")
        self.scope_box.addItem(f"Весь блок — {self.batch.total}", "all")
        if not waiting:
            self.scope_box.setCurrentIndex(1 if printed else 2)
        if self._fixed_range is not None:
            first, last = self._fixed_range
            self.scope_box.clear()
            self.scope_box.addItem(f"Заменяющие коды — {last - first}", "fixed")
            self.scope_box.setEnabled(False)
            self.kind_box.setCurrentIndex(self.kind_box.findData("remark"))
            self.kind_box.setEnabled(False)
        form.addRow("Какие коды", self.scope_box)
        root.addLayout(form)

        # Поля перемаркировки. Остатки их не показывают.
        self.remark_box = QWidget(self)
        remark = QFormLayout(self.remark_box)
        remark.setContentsMargins(0, 0, 0, 0)
        remark.setSpacing(9)
        self.cause_box = QComboBox(self.remark_box)
        for code, text in introduce.REMARK_CAUSES.items():
            self.cause_box.addItem(f"{code} — {text}", code)
        self.cause_box.setCurrentIndex(
            max(0, self.cause_box.findData(self._settings.remark_cause)))
        remark.addRow("Причина", self.cause_box)
        self.date_edit = QDateEdit(QDate.currentDate(), self.remark_box)
        self.date_edit.setDisplayFormat("dd.MM.yyyy")
        self.date_edit.setCalendarPopup(True)
        remark.addRow("Дата перемаркировки", self.date_edit)

        attributes = QHBoxLayout()
        attributes.setSpacing(9)
        self.country_edit = QLineEdit(self.batch.country, self.remark_box)
        self.country_edit.setPlaceholderText("643")
        self.country_edit.setMaxLength(3)
        self.country_edit.setMaximumWidth(80)
        self.color_edit = QLineEdit(self.batch.color, self.remark_box)
        self.color_edit.setPlaceholderText("Цвет")
        self.size_edit = QLineEdit(self.batch.size, self.remark_box)
        self.size_edit.setPlaceholderText("Размер")
        self.size_edit.setMaximumWidth(120)
        attributes.addWidget(QLabel("Страна (ОКСМ)"))
        attributes.addWidget(self.country_edit)
        attributes.addWidget(self.color_edit, 1)
        attributes.addWidget(self.size_edit)
        remark.addRow("Сведения о товаре", attributes)

        self.previous_edit = QPlainTextEdit(self.remark_box)
        self.previous_edit.setPlaceholderText(
            "Предыдущие КИ — по одному в строке, в том же порядке, что и новые. "
            "Для причины KM_SPOILED можно оставить пустым")
        self.previous_edit.setMaximumHeight(70)
        if self._previous:
            self.previous_edit.setPlainText("\n".join(self._previous))
        remark.addRow("Предыдущие КИ", self.previous_edit)

        document = QHBoxLayout()
        document.setSpacing(9)
        self.doc_type_box = QComboBox(self.remark_box)
        self.doc_type_box.addItem("— нет —", "")
        for code, text in introduce.PRIMARY_DOCUMENT_TYPES.items():
            self.doc_type_box.addItem(text, code)
        self.doc_number_edit = QLineEdit(self.remark_box)
        self.doc_number_edit.setPlaceholderText("Номер")
        self.doc_date_edit = QLineEdit(self.remark_box)
        self.doc_date_edit.setPlaceholderText("ГГГГ-ММ-ДД")
        self.doc_date_edit.setMaximumWidth(110)
        self.doc_name_edit = QLineEdit(self.remark_box)
        self.doc_name_edit.setPlaceholderText("Наименование (для иного)")
        document.addWidget(self.doc_type_box)
        document.addWidget(self.doc_number_edit)
        document.addWidget(self.doc_date_edit)
        document.addWidget(self.doc_name_edit, 1)
        remark.addRow("Первичный документ", document)
        root.addWidget(self.remark_box)

        self.history = Hint(self._history_text())
        self.history.setWordWrap(True)
        root.addWidget(self.history)
        self.hint = Hint("")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        row = QHBoxLayout()
        row.setSpacing(9)
        self.file_button = QPushButton("Сохранить файл", self)
        self.file_button.setIcon(icons.icon("save"))
        self.file_button.setAutoDefault(False)
        self.file_button.clicked.connect(self._save_file)
        row.addWidget(self.file_button)
        row.addStretch(1)
        self.send_button = QPushButton("Отправить в «Честный ЗНАК»", self)
        self.send_button.setObjectName("Primary")
        self.send_button.setIcon(icons.icon("export"))
        self.send_button.setAutoDefault(False)
        self.send_button.clicked.connect(self._send)
        row.addWidget(self.send_button)
        close = QPushButton("Закрыть", self)
        close.setDefault(True)
        close.clicked.connect(self.reject)
        row.addWidget(close)
        root.addLayout(row)

        for control in (self.inn_edit, self.tnved_edit, self.country_edit,
                        self.color_edit, self.size_edit, self.doc_number_edit,
                        self.doc_date_edit, self.doc_name_edit):
            control.textChanged.connect(self._refresh)
        self.previous_edit.textChanged.connect(self._refresh)
        for box in (self.scope_box, self.cause_box, self.doc_type_box):
            box.currentIndexChanged.connect(self._refresh)
        self.date_edit.dateChanged.connect(self._refresh)
        self.kind_box.currentIndexChanged.connect(self._on_kind_changed)

    def _history_text(self) -> str:
        if not self.batch.doc_id:
            return ""
        code = self.batch.doc_status
        status = introduce.STATUS_TITLES.get(code.upper(), code) if code \
            else "статус неизвестен"
        return (f"Уже отправлялся документ {self.batch.doc_id} на "
                f"{self.batch.introduced_count} кодов — {status}. Одни и те же "
                "коды повторно система не примет.")

    # --- вид документа ------------------------------------------------------------------

    @property
    def kind(self) -> str:
        return str(self.kind_box.currentData() or "remark")

    def _on_kind_changed(self) -> None:
        self.remark_box.setVisible(self.kind == "remark")
        self.adjustSize()
        self._refresh()

    @property
    def _send_problem(self) -> str:
        return self._send_problems.get(self.kind, "")

    # --- выбор кодов ----------------------------------------------------------------------

    def _range(self) -> tuple[int, int]:
        """Какие коды блока берутся: [от, до)."""
        if self._fixed_range is not None:
            return self._fixed_range
        printed = min(self.batch.printed, self.batch.total)
        sent = min(self.batch.introduced_count, self.batch.total)
        scope = self.scope_box.currentData()
        if scope == "pending":
            return sent, printed
        if scope == "printed":
            return 0, printed
        return 0, self.batch.total

    def _codes(self) -> list[str]:
        start, end = self._range()
        return self.batch.codes[start:end]

    def _document(self):
        codes = tuple(self._codes())
        if self.kind == "ostatky":
            return introduce.Document(self.inn_edit.text().strip(),
                                      self.tnved_edit.text().strip(), codes)
        previous = tuple(line.strip() for line in
                         self.previous_edit.toPlainText().splitlines() if line.strip())
        return introduce.Remark(
            inn=self.inn_edit.text().strip(), tnved=self.tnved_edit.text().strip(),
            cause=str(self.cause_box.currentData() or ""),
            date=self.date_edit.date().toString("yyyy-MM-dd"), codes=codes,
            previous=previous, country=self.country_edit.text().strip(),
            color=self.color_edit.text().strip(), size=self.size_edit.text().strip(),
            doc_type=str(self.doc_type_box.currentData() or ""),
            doc_number=self.doc_number_edit.text().strip(),
            doc_date=self.doc_date_edit.text().strip(),
            doc_name=self.doc_name_edit.text().strip())

    @property
    def document(self):
        return self._document()

    # --- состояние --------------------------------------------------------------------------

    def _refresh(self, *_args) -> None:
        if not hasattr(self, "hint"):
            return
        problems = self._document().problems
        self.file_button.setEnabled(not problems)
        self.send_button.setEnabled(not problems and not self._send_problem)
        if problems:
            self.hint.setStyleSheet(f"color: {Palette.WARNING};")
            self.hint.setText("Пока нельзя: " + "; ".join(problems) + ".")
            return
        if self._send_problem:
            self.hint.setStyleSheet(f"color: {Palette.WARNING};")
            self.hint.setText(
                f"Отправить из программы сейчас нельзя: {self._send_problem} "
                "Файл можно сохранить и загрузить в личном кабинете.")
            return
        self.hint.setStyleSheet("")
        if self.kind == "remark":
            self.hint.setText(
                "Документ будет подписан сертификатом и отправлен в «Честный ЗНАК»; "
                "КриптоПро может спросить пароль. Новые коды должны быть в статусе "
                "«Эмитирован. Получен» с типом эмиссии «Перемаркировка».")
        else:
            self.hint.setText(
                "Документ будет подписан сертификатом и отправлен в «Честный ЗНАК»; "
                "КриптоПро может спросить пароль. Вводить в оборот нужно только то, "
                "что уже наклеено.")

    # --- действия ------------------------------------------------------------------------------

    def _fail(self, message: str) -> None:
        self.hint.setStyleSheet(f"color: {Palette.DANGER};")
        self.hint.setText(message)

    def _remember(self, document) -> None:
        """Запоминает выбор и сведения о товаре: в следующий раз вводить не придётся."""
        self.batch.tnved = document.tnved
        if document.kind == "remark":
            self.batch.country = document.country
            self.batch.color = document.color
            self.batch.size = document.size
            self._settings.remark_cause = document.cause
        self._settings.introduce_kind = document.kind
        try:
            labels.save(self._settings)
        except OSError:
            pass

    def _save_file(self) -> None:
        document = self._document()
        try:
            document.render(".json" if document.kind == "remark" else ".xml")
        except introduce.IntroduceProblem as error:
            self._fail(str(error))
            return
        default = introduce.file_name(
            self.batch.gtin, extension=".json" if document.kind == "remark" else ".xml",
            title=document.title)
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить файл ввода в оборот",
            os.path.join(os.path.expanduser("~"), default), document.file_filters)
        if not path:
            return
        extension = os.path.splitext(path)[1].lower() or (
            ".json" if document.kind == "remark" else ".xml")
        try:
            text = document.render(extension)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except (OSError, introduce.IntroduceProblem) as error:
            self._fail(f"Файл не сохранён: {error}")
            return
        self._remember(document)
        issue.mark_introduced(self.batch)
        self.saved_path = path
        self.action = "file"
        self.accept()

    def _confirm_send(self, document) -> bool:
        where = ("БОЕВОЙ контур — документ юридически значим"
                 if self._contour.value == "production" else "песочница")
        extra = ""
        if self.batch.introduced_count and self.scope_box.currentData() != "pending":
            extra = ("\n\nЧасть этих кодов уже отправлялась документом "
                     f"{self.batch.doc_id}: повторно система их не примет.")
        reason = ""
        if document.kind == "remark":
            reason = f"\nПричина: {document.cause} — {introduce.REMARK_CAUSES[document.cause]}."
        box = QMessageBox(QMessageBox.Icon.Question, "Отправить документ?",
                          f"Отправить документ «{document.title}»: "
                          f"{len(document.codes)} кодов под ИНН {document.inn}?"
                          f"{reason}\n\nКонтур: {where}.\nОтменить отправленный "
                          f"документ нельзя.{extra}", parent=self)
        send = box.addButton("Отправить", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is send

    def _send(self) -> None:
        document = self._document()
        if document.problems or self._send_problem:
            self._refresh()
            return
        if not self._confirm_send(document):
            return
        self._remember(document)
        issue.save(self.batch)
        start, end = self._range()
        # Блок отмечается «введён» с начала и до `covered`. Замена одного кода
        # из середины блока не вправе объявить введёнными напечатанные до него,
        # но ещё не отправленные: они остаются в очереди.
        self.covered = end if self._fixed_range is None \
            or start <= self.batch.introduced_count else self.batch.introduced_count
        self.action = "send"
        self.accept()
