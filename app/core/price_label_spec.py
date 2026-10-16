"""Макет ценника-этикетки и запоминание настроек печати на этом рабочем месте.

Размер по умолчанию — 30×20 мм: так обклеивают товар. Сверху название с
характеристикой, ниже цена жирным, под ней штрихкод с цифрами. Вертикальные
размеры не задаются числами по отдельности, а складываются стопкой от верхнего
поля: изменили шрифт цены — штрихкод сам сдвинулся вниз, ничего не наезжает.

Настройки лежат отдельным файлом, как и у печати маркировки: это то, как
настроен принтер конкретного компьютера, а не предпочтения окна.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields

from . import appdata

FILE_NAME = "price_labels.json"

_PT = 25.4 / 72  # миллиметров в пункте

# Запас до нижней кромки этикетки, ниже штрихкода и зоны названия не сжимаем.
EDGE = 0.2
MIN_BAR = 3.0
MIN_NAME = 4.0

# Что делать между номенклатурами: печатать подряд, ждать, пока оторвут
# напечатанное, или вставлять пустую этикетку-разделитель.
GAP_CONTINUOUS = "continuous"
GAP_TEAR = "tear"
GAP_BLANK = "blank"
GAP_MODES = (GAP_CONTINUOUS, GAP_TEAR, GAP_BLANK)


@dataclass(frozen=True, slots=True)
class Layout:
    """Верхние края блоков этикетки в мм от верха, сдвиг уже учтён."""

    name_top: float
    name_height: float
    price_top: float
    bar_top: float
    bar_height: float
    digits_top: float
    # Содержимое всё равно выходит за край — на печати часть пропадёт.
    clipped: bool


@dataclass(slots=True)
class PriceLabelSpec:
    """Размеры в миллиметрах, шрифты в пунктах."""

    width: float = 30.0
    height: float = 20.0
    margin: float = 1.0
    # Высота, отведённая названию. Шрифт названия подбирается сам — от
    # заданного вниз, пока текст не поместится; длинное «Куб с вопросом для
    # мальчика» иначе обрезалось бы.
    name_height: float = 7.0
    name_font: float = 5.5
    name_font_min: float = 3.5
    price_font: float = 9.0
    price_suffix: str = " ₽"
    # Сколько цен на этикетке: 1 — крупная цена; 2 — две строки мелким шрифтом:
    # «Цена: 469» из файла и «Цена с макс. скидкой: 422», которую считает программа.
    prices: int = 1
    price_caption: str = "Цена:"
    discount_caption: str = "Цена с макс. скидкой:"
    bar_height: float = 4.6
    show_digits: bool = True
    digits_font: float = 5.0
    # Подгонка под принтер: многие сдвигают печать на долю миллиметра.
    offset_x: float = 0.0
    offset_y: float = 0.0

    @property
    def price_line(self) -> float:
        """Высота блока цены: одна строка или две."""
        return self.price_font * _PT * 1.15 * (2 if self.prices == 2 else 1)

    @property
    def digits_line(self) -> float:
        return self.digits_font * _PT * 1.2 if self.show_digits else 0.0

    @property
    def name_top(self) -> float:
        return self.margin

    @property
    def price_top(self) -> float:
        return self.name_top + self.name_height

    @property
    def bar_top(self) -> float:
        return self.price_top + self.price_line + 0.3

    @property
    def digits_top(self) -> float:
        return self.bar_top + self.bar_height + 0.1

    @property
    def bottom(self) -> float:
        return self.digits_top + self.digits_line

    def layout(self) -> "Layout":
        """Вертикальная раскладка с учётом сдвига вниз/вверх.

        Сдвиг двигает всё содержимое, и низ этикетки с цифрами штрихкода уходил
        за край: на термоленте это просто пустое место, и человек видел, как
        строка пропадает. Теперь при сдвиге вниз сначала сжимается штрихкод (не
        ниже 3 мм, иначе сканер его не читает), потом зона названия — шрифт в ней
        подберётся сам. Если и так не помещается, `clipped` говорит об этом, и
        страница предупреждает, а не молчит.
        """
        top = self.margin + self.offset_y
        name_height, bar_height = self.name_height, self.bar_height
        # Запас до края: печать впритык к кромке ленты принтеры обрезают.
        limit = self.height - EDGE
        bottom = (top + name_height + self.price_line + 0.3 + bar_height + 0.1
                  + self.digits_line)
        excess = bottom - limit
        if excess > 0:
            cut = min(excess, max(0.0, bar_height - MIN_BAR))
            bar_height -= cut
            excess -= cut
        if excess > 0:
            cut = min(excess, max(0.0, name_height - MIN_NAME))
            name_height -= cut
            excess -= cut
        price_top = top + name_height
        bar_top = price_top + self.price_line + 0.3
        return Layout(
            name_top=top, name_height=name_height, price_top=price_top,
            bar_top=bar_top, bar_height=bar_height,
            digits_top=bar_top + bar_height + 0.1,
            clipped=excess > 0.001 or top < 0)

    @property
    def problems(self) -> list[str]:
        found: list[str] = []
        if self.width < 15 or self.height < 10:
            found.append("этикетка меньше 15×10 мм")
        if self.bottom + self.margin > self.height + 0.01:
            found.append(
                f"содержимое не помещается по высоте: нужно {self.bottom + self.margin:.1f} мм, "
                f"а этикетка {self.height:.1f} мм")
        if self.bar_height < 3:
            found.append("штрихкод ниже 3 мм сканер читает плохо")
        return found


# --- шаблоны ---------------------------------------------------------------------
#
# Этикетки бывают разных размеров и с одной или двумя ценами, поэтому макет —
# не один на программу, а выбирается по имени. Встроенные шаблоны есть всегда: их
# можно подправить под свой принтер, и правка сохранится, а «Сбросить» вернёт
# заводские размеры. Свои шаблоны создаются копией существующего.

T_30X20_1 = "30x20_1цена"
T_30X20_2 = "30x20_2цены"
T_43X25_1 = "43x25_1цена"
T_43X25_2 = "43x25_2цены"
DEFAULT_TEMPLATE = T_30X20_1
BUILTIN_TEMPLATES = (T_30X20_1, T_30X20_2, T_43X25_1, T_43X25_2)


def builtin(name: str) -> PriceLabelSpec:
    """Заводской вариант встроенного шаблона."""
    if name == T_30X20_1:
        return PriceLabelSpec()
    if name == T_30X20_2:
        return PriceLabelSpec(prices=2, price_font=6.0, price_suffix="",
                              name_height=6.0, bar_height=4.2)
    big = dict(width=43.0, height=25.0, name_height=8.0, name_font=6.5,
               price_font=12.0, bar_height=5.5, digits_font=6.0)
    if name == T_43X25_1:
        return PriceLabelSpec(**big)
    if name == T_43X25_2:
        return PriceLabelSpec(**{**big, "price_font": 7.5}, prices=2, price_suffix="")
    raise KeyError(name)


def _spec_from(saved: object, base: PriceLabelSpec) -> PriceLabelSpec:
    """Макет из сохранённых полей поверх `base`. Непонятное значение пропускается."""
    if not isinstance(saved, dict):
        return base
    for item in fields(PriceLabelSpec):
        if item.name not in saved:
            continue
        current = getattr(base, item.name)
        value = saved[item.name]
        try:
            if isinstance(current, bool):
                setattr(base, item.name, bool(value))
            elif isinstance(current, str):
                setattr(base, item.name, str(value))
            elif isinstance(current, int):
                setattr(base, item.name, int(float(value)))
            else:
                setattr(base, item.name, float(value))
        except (TypeError, ValueError):
            pass
    if base.prices not in (1, 2):
        base.prices = 1
    return base


@dataclass(slots=True)
class PrintSettings:
    printer: str = ""
    # Последний открытый файл: номенклатура одна и та же от печати к печати.
    last_file: str = ""
    # Режим между номенклатурами: см. GAP_MODES.
    gap_mode: str = GAP_CONTINUOUS
    # Выбранный шаблон и правки шаблонов: имя → макет. Тут и свои шаблоны, и
    # исправленные встроенные; не тронутые встроенные берутся из кода.
    template: str = DEFAULT_TEMPLATE
    templates: dict[str, PriceLabelSpec] = field(default_factory=dict)
    # Скидка для шаблонов с двумя ценами, в процентах.
    discount: float = 10.0

    def names(self) -> list[str]:
        """Встроенные шаблоны по порядку, затем свои — по алфавиту."""
        custom = sorted(n for n in self.templates if n not in BUILTIN_TEMPLATES)
        return [*BUILTIN_TEMPLATES, *custom]

    @staticmethod
    def is_builtin(name: str) -> bool:
        return name in BUILTIN_TEMPLATES

    def spec(self, name: str | None = None) -> PriceLabelSpec:
        name = name or self.template
        if name in self.templates:
            return self.templates[name]
        if name in BUILTIN_TEMPLATES:
            return builtin(name)
        return builtin(DEFAULT_TEMPLATE)

    def select(self, name: str) -> PriceLabelSpec:
        """Делает шаблон текущим и возвращает его макет (правки живут в нём же)."""
        if name not in self.names():
            name = DEFAULT_TEMPLATE
        self.template = name
        self.templates[name] = self.spec(name)
        return self.templates[name]

    def add(self, name: str, spec: PriceLabelSpec) -> None:
        self.templates[name] = spec

    def remove(self, name: str) -> bool:
        """Удаляет свой шаблон. Встроенный не удаляется — его можно только сбросить."""
        if self.is_builtin(name) or name not in self.templates:
            return False
        del self.templates[name]
        if self.template == name:
            self.template = DEFAULT_TEMPLATE
        return True

    def reset(self, name: str) -> PriceLabelSpec:
        """Возвращает встроенному шаблону заводские размеры."""
        self.templates[name] = builtin(name)
        return self.templates[name]


def path() -> str:
    return appdata.path_to(FILE_NAME)


def load() -> PrintSettings:
    """Сохранённые настройки. Испорченный файл — то же, что его отсутствие."""
    try:
        with open(path(), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return PrintSettings()
    if not isinstance(raw, dict):
        return PrintSettings()
    templates: dict[str, PriceLabelSpec] = {}
    saved = raw.get("templates")
    if isinstance(saved, dict):
        for name, values in saved.items():
            if not isinstance(name, str) or not name.strip():
                continue
            base = builtin(name) if name in BUILTIN_TEMPLATES else PriceLabelSpec()
            templates[name] = _spec_from(values, base)
    elif isinstance(raw.get("label"), dict):
        # Файл версии, где шаблон был один: его настройки принадлежат 30×20.
        templates[DEFAULT_TEMPLATE] = _spec_from(raw["label"], PriceLabelSpec())
    settings = PrintSettings(
        printer=str(raw.get("printer") or ""), last_file=str(raw.get("last_file") or ""),
        gap_mode=mode if (mode := raw.get("gap_mode")) in GAP_MODES else GAP_CONTINUOUS,
        templates=templates)
    chosen = str(raw.get("template") or DEFAULT_TEMPLATE)
    settings.template = chosen if chosen in settings.names() else DEFAULT_TEMPLATE
    try:
        percent = float(raw.get("discount", settings.discount))
        if 0 < percent < 100:
            settings.discount = percent
    except (TypeError, ValueError):
        pass
    return settings


def save(settings: PrintSettings) -> None:
    payload = {
        "printer": settings.printer, "last_file": settings.last_file,
        "gap_mode": settings.gap_mode, "template": settings.template,
        "discount": settings.discount,
        "templates": {name: asdict(spec) for name, spec in settings.templates.items()},
    }
    target = path()
    if directory := os.path.dirname(target):
        os.makedirs(directory, exist_ok=True)
    temporary = f"{target}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, target)
