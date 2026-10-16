"""Окна ценников: настройка шаблона этикетки и скидка для шаблона с двумя ценами."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core import price_label_spec as spec_store
from ...core import price_labels
from ...core.price_label_spec import PriceLabelSpec
from ...core.price_labels import PriceLabel
from .. import price_label_print
from ..theme import Palette
from .inputs import SelectBox

SAMPLE = PriceLabel(0, "Бомбочка для ванны Cosmo 230 г", "тигр", "25891146",
                    Decimal(469), 1)


def preview_zoom(spec: PriceLabelSpec) -> int:
    """Увеличение предпросмотра: этикетка 30 мм — вдвое, шире — как есть."""
    return 2 if spec.width * 8 * 2 <= 500 else 1


def _percent_text(value: float) -> str:
    return f"{value:g}".replace(".", ",")


class DiscountDialog(QDialog):
    """Какую скидку применить к номенклатуре — для шаблона с двумя ценами."""

    def __init__(self, percent: float, sample: Decimal | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Скидка")
        self.setModal(True)
        self._sample = sample

        root = QVBoxLayout(self)
        root.setSpacing(10)
        intro = QLabel(
            "На этикетке будет две цены: прежняя зачёркнута, рядом цена со "
            "скидкой. Какую скидку (в процентах) применить к номенклатуре?", self)
        intro.setWordWrap(True)
        intro.setMinimumWidth(360)
        root.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Скидка:", self))
        self.spin = QDoubleSpinBox(self)
        self.spin.setRange(1, 99)
        self.spin.setDecimals(1)
        self.spin.setSingleStep(1)
        self.spin.setSuffix(" %")
        self.spin.setValue(percent if 0 < percent < 100 else 10)
        self.spin.valueChanged.connect(self._example)
        row.addWidget(self.spin)
        row.addStretch(1)
        root.addLayout(row)

        self.example = QLabel("", self)
        self.example.setObjectName("Hint")
        root.addWidget(self.example)
        self._example()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Применить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.spin.setFocus()
        self.spin.selectAll()

    @property
    def percent(self) -> float:
        return float(self.spin.value())

    def _example(self) -> None:
        if self._sample is None:
            self.example.setText("")
            return
        after = price_labels.discounted(self._sample, self.percent)
        self.example.setText(
            f"Например: {price_labels.format_price(self._sample)} ₽ → "
            f"{price_labels.format_price(after)} ₽")


class TemplateDialog(QDialog):
    """Размеры и шрифты шаблона с живым предпросмотром."""

    def __init__(self, name: str, spec: PriceLabelSpec, *, builtin: bool,
                 sample: PriceLabel | None, percent: float,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Шаблон «{name}»")
        self.setModal(True)
        self._name = name
        self._base = replace(spec)
        self._percent = percent
        self._sample = replace(sample or SAMPLE, copies=1)
        self._ready = False

        root = QVBoxLayout(self)
        root.setSpacing(10)

        self.preview = QLabel(self)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(220)
        self.preview.setStyleSheet(f"background: {Palette.SURFACE_ALT};")
        root.addWidget(self.preview)
        self.note = QLabel("", self)
        self.note.setWordWrap(True)
        root.addWidget(self.note)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(6)
        self.width_ = self._spin(spec.width, 15, 200)
        self.height_ = self._spin(spec.height, 10, 200)
        self.margin = self._spin(spec.margin, 0, 10)
        self.prices = SelectBox(self)
        self.prices.addItem("Одна цена", 1)
        self.prices.addItem("Две цены: обычная и со скидкой", 2)
        self.prices.setCurrentIndex(0 if spec.prices == 1 else 1)
        self.prices.currentIndexChanged.connect(self._changed)
        self.name_height = self._spin(spec.name_height, 2, 40)
        self.name_font = self._spin(spec.name_font, 3, 24, suffix=" пт")
        self.price_font = self._spin(spec.price_font, 5, 48, suffix=" пт")
        self.caption = QLineEdit(spec.price_caption, self)
        self.caption.setMaxLength(30)
        self.caption.textChanged.connect(self._changed)
        self.discount_caption = QLineEdit(spec.discount_caption, self)
        self.discount_caption.setMaxLength(30)
        self.discount_caption.textChanged.connect(self._changed)
        self.suffix = QLineEdit(spec.price_suffix.strip(), self)
        self.suffix.setMaxLength(8)
        self.suffix.setMaximumWidth(80)
        self.suffix.textChanged.connect(self._changed)
        self.bar_height = self._spin(spec.bar_height, 3, 40)
        self.digits = QCheckBox("Печатать цифры под штрихкодом", self)
        self.digits.setChecked(spec.show_digits)
        self.digits.toggled.connect(self._changed)
        self.digits_font = self._spin(spec.digits_font, 3, 14, suffix=" пт")

        size = QHBoxLayout()
        size.addWidget(self.width_)
        size.addWidget(QLabel("×", self))
        size.addWidget(self.height_)
        size.addStretch(1)
        form.addRow("Размер этикетки, мм", size)
        form.addRow("Поля, мм", self.margin)
        form.addRow("Цены", self.prices)
        form.addRow("Высота названия, мм", self.name_height)
        form.addRow("Шрифт названия", self.name_font)
        form.addRow("Шрифт цены", self.price_font)
        self.caption_label = QLabel("Подпись цены", self)
        form.addRow(self.caption_label, self.caption)
        self.discount_caption_label = QLabel("Подпись цены со скидкой", self)
        form.addRow(self.discount_caption_label, self.discount_caption)
        form.addRow("Знак после цены", self.suffix)
        form.addRow("Высота штрихкода, мм", self.bar_height)
        form.addRow("", self.digits)
        form.addRow("Шрифт цифр", self.digits_font)
        root.addLayout(form)

        buttons = QDialogButtonBox(self)
        self.ok = buttons.addButton("Сохранить", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        if builtin:
            reset = QPushButton("Заводские размеры", self)
            reset.setToolTip("Вернуть этому шаблону размеры и шрифты, с которыми он поставляется")
            reset.clicked.connect(self._factory)
            buttons.addButton(reset, QDialogButtonBox.ButtonRole.ResetRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._ready = True
        self._changed()

    def _spin(self, value: float, low: float, high: float, suffix: str = "") -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setRange(low, high)
        spin.setDecimals(1)
        spin.setSingleStep(0.5)
        spin.setValue(value)
        if suffix:
            spin.setSuffix(suffix)
        spin.valueChanged.connect(self._changed)
        return spin

    def spec(self) -> PriceLabelSpec:
        """Макет по тому, что сейчас в полях. Сдвиг печати и прочее скрытое — как было."""
        suffix = self.suffix.text().strip()
        return replace(
            self._base, width=self.width_.value(), height=self.height_.value(),
            margin=self.margin.value(), prices=int(self.prices.currentData()),
            name_height=self.name_height.value(), name_font=self.name_font.value(),
            price_font=self.price_font.value(),
            price_caption=self.caption.text().strip(),
            discount_caption=self.discount_caption.text().strip(),
            price_suffix=f" {suffix}" if suffix else "", bar_height=self.bar_height.value(),
            show_digits=self.digits.isChecked(), digits_font=self.digits_font.value())

    def _factory(self) -> None:
        factory = spec_store.builtin(self._name)
        self._base = replace(self._base, name_font_min=factory.name_font_min,
                             offset_x=factory.offset_x, offset_y=factory.offset_y)
        self._ready = False
        self.width_.setValue(factory.width)
        self.height_.setValue(factory.height)
        self.margin.setValue(factory.margin)
        self.prices.setCurrentIndex(0 if factory.prices == 1 else 1)
        self.name_height.setValue(factory.name_height)
        self.name_font.setValue(factory.name_font)
        self.price_font.setValue(factory.price_font)
        self.caption.setText(factory.price_caption)
        self.discount_caption.setText(factory.discount_caption)
        self.suffix.setText(factory.price_suffix.strip())
        self.bar_height.setValue(factory.bar_height)
        self.digits.setChecked(factory.show_digits)
        self.digits_font.setValue(factory.digits_font)
        self._ready = True
        self._changed()

    def _changed(self, *_args) -> None:
        if not self._ready:
            return
        spec = self.spec()
        two = spec.prices == 2
        for widget in (self.caption, self.caption_label, self.discount_caption,
                       self.discount_caption_label):
            widget.setVisible(two)
        self.digits_font.setEnabled(spec.show_digits)
        sample = replace(self._sample)
        if two and sample.price is not None:
            sample.price2 = price_labels.discounted(sample.price, self._percent)
        problems = spec.problems
        if not price_label_print.fits(sample, spec):
            problems.append("штрихкод не помещается по ширине")
        try:
            image = price_label_print.preview(spec, sample, zoom=preview_zoom(spec))
            self.preview.setPixmap(QPixmap.fromImage(image))
        except (ValueError, price_label_print.PrintError):
            self.preview.clear()
        self.ok.setEnabled(not problems)
        self.note.setStyleSheet(f"color: {Palette.DANGER};" if problems else "")
        self.note.setText(
            "; ".join(problems) if problems else
            "Предпросмотр на примере: так этикетка выйдет с принтера на 203 dpi.")
