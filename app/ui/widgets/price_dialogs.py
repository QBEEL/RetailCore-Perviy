"""Диалог настроек переоценки: поля поиска, порог совпадения, разделители.

Профили поставщиков жили здесь же, пока не появилась база: теперь их место —
вкладка «Поставщики», где к структуре прайса добавились карточка и привязки.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ...core.article import DEFAULT_MODIFIER_SEPARATORS, DEFAULT_SEPARATORS
from ...core.pricing import MatchOptions
from ..theme import Metrics, Palette
from .common import Divider, Hint, SectionTitle

# Подпись, подсказка и имя поля настроек. Порядок — как в постановке задачи.
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("use_article", "Поиск по артикулу",
     "Главный ключ. Ячейка 1С может перечислять несколько артикулов одной "
     "номенклатуры — проверяются все."),
    ("use_sku", "Поиск по внутреннему коду",
     "Код номенклатуры 1С, если он есть в обоих файлах."),
    ("use_ean", "Поиск по штрихкоду",
     "В выгрузке 1С штрихкода обычно нет — тогда этап ничего не даёт."),
    ("use_name", "Поиск по названию",
     "Точное совпадение названия без объёма и повторяющихся слов."),
    ("use_fuzzy", "Использовать Fuzzy Match",
     "Похожие названия с опечатками. Автоматически не применяется никогда: "
     "варианты показываются для выбора вручную."),
    ("ignore_case", "Игнорировать регистр", "«ZRP0010» и «zrp0010» — один артикул."),
    ("ignore_spaces", "Игнорировать пробелы", "«ZRP 0010» и «ZRP0010» — один артикул."),
    ("ignore_symbols", "Игнорировать специальные символы",
     "«ZRP-0010» и «ZRP0010» — один артикул. Без этого дефис и точка значимы."),
)


class PriceSettingsDialog(QDialog):
    """Все настройки поиска товара для переоценки в одном окне."""

    def __init__(
        self,
        options: MatchOptions,
        skip_unchanged: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки переоценки")
        self.setMinimumWidth(560)
        self._options = options
        self._boxes: dict[str, QCheckBox] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(Metrics.PAD + 4, Metrics.PAD, Metrics.PAD + 4, Metrics.PAD)
        root.setSpacing(Metrics.GAP)

        root.addWidget(SectionTitle("Итоговый файл", self))
        self.skip_box = QCheckBox("Исключать товары без изменения цены", self)
        self.skip_box.setChecked(skip_unchanged)
        self.skip_box.setToolTip(
            "В файл попадут только строки, в которые записана новая цена.\n"
            "Ненайденные и требующие сопоставления строки тоже будут убраны,\n"
            "поэтому 1С не увидит их в этой загрузке.")
        root.addWidget(self.skip_box)
        root.addWidget(Divider(self))

        root.addWidget(SectionTitle("По каким полям искать товар", self))
        grid = QGridLayout()
        grid.setHorizontalSpacing(Metrics.GAP + 6)
        grid.setVerticalSpacing(2)
        for position, (name, label, hint) in enumerate(_FIELDS):
            box = QCheckBox(label, self)
            box.setChecked(bool(getattr(options, name)))
            box.setToolTip(hint)
            self._boxes[name] = box
            grid.addWidget(box, position % 4, position // 4)
        root.addLayout(grid)
        root.addWidget(Divider(self))

        root.addWidget(SectionTitle("Минимальный процент совпадения", self))
        row = QHBoxLayout()
        row.setSpacing(Metrics.GAP)
        self.score = QSlider(Qt.Orientation.Horizontal, self)
        self.score.setRange(50, 100)
        self.score.setValue(int(round(options.min_score)))
        self.score.setTickInterval(5)
        self.score.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.score.valueChanged.connect(self._show_score)
        row.addWidget(self.score, 1)
        self.score_label = QLabel(self)
        self.score_label.setMinimumWidth(46)
        self.score_label.setStyleSheet(f"font-weight: 600; color: {Palette.PRIMARY};")
        row.addWidget(self.score_label)
        root.addLayout(row)
        root.addWidget(Hint(
            "Ниже этого порога совпадение не применяется само, а уходит в "
            "«Требует сопоставления» со списком вариантов.", self))
        self._show_score(self.score.value())

        root.addWidget(Divider(self))
        root.addWidget(SectionTitle("Разделители в артикуле", self))
        fields = QGridLayout()
        fields.setHorizontalSpacing(Metrics.GAP)
        fields.setVerticalSpacing(4)
        self.separators = QLineEdit(options.separators, self)
        self.separators.setToolTip(
            "Символы, которыми в одной ячейке перечислены разные артикулы:\n"
            "«zrp0050perG32/zrp0010perG32». Каждый проверяется отдельно.")
        self.modifiers = QLineEdit(options.modifier_separators, self)
        self.modifiers.setToolTip(
            "Символы, отделяющие модификацию от базового артикула:\n"
            "«ABC123/50», «Cream-01_50ml». Если точного артикула нет,\n"
            "поиск идёт по базе, а нужный вариант выбирается по объёму.")
        fields.addWidget(QLabel("Перечисление артикулов:", self), 0, 0)
        fields.addWidget(self.separators, 0, 1)
        fields.addWidget(QLabel("Модификация (объём, размер, цвет):", self), 1, 0)
        fields.addWidget(self.modifiers, 1, 1)
        fields.setColumnStretch(1, 1)
        root.addLayout(fields)

        root.addStretch(1)
        buttons = QDialogButtonBox(self)
        reset = buttons.addButton("По умолчанию", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(self._reset)
        buttons.addButton(QDialogButtonBox.StandardButton.Ok).setText("Применить")
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _show_score(self, value: int) -> None:
        self.score_label.setText(f"{value} %")

    def _reset(self) -> None:
        defaults = MatchOptions()
        for name, box in self._boxes.items():
            box.setChecked(bool(getattr(defaults, name)))
        self.score.setValue(int(round(defaults.min_score)))
        self.separators.setText(defaults.separators)
        self.modifiers.setText(defaults.modifier_separators)
        self.skip_box.setChecked(False)

    def apply_to(self, options: MatchOptions) -> None:
        """Переносит выбор в настройки. Пустое поле разделителей — значение по умолчанию."""
        for name, box in self._boxes.items():
            setattr(options, name, box.isChecked())
        options.min_score = float(self.score.value())
        options.separators = self.separators.text().strip() or DEFAULT_SEPARATORS
        options.modifier_separators = self.modifiers.text().strip() or DEFAULT_MODIFIER_SEPARATORS

    @property
    def skip_unchanged(self) -> bool:
        return self.skip_box.isChecked()
