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
from dataclasses import asdict, dataclass, fields

from . import appdata

FILE_NAME = "price_labels.json"

_PT = 25.4 / 72  # миллиметров в пункте


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
    bar_height: float = 4.6
    show_digits: bool = True
    digits_font: float = 5.0
    # Подгонка под принтер: многие сдвигают печать на долю миллиметра.
    offset_x: float = 0.0
    offset_y: float = 0.0

    @property
    def price_line(self) -> float:
        return self.price_font * _PT * 1.15

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


@dataclass(slots=True)
class PrintSettings:
    printer: str = ""
    label: PriceLabelSpec | None = None
    # Последний открытый файл: номенклатура одна и та же от печати к печати.
    last_file: str = ""

    def spec(self) -> PriceLabelSpec:
        return self.label or PriceLabelSpec()


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
    spec = PriceLabelSpec()
    saved = raw.get("label")
    if isinstance(saved, dict):
        for item in fields(PriceLabelSpec):
            if item.name not in saved:
                continue
            current = getattr(spec, item.name)
            value = saved[item.name]
            try:
                if isinstance(current, bool):
                    setattr(spec, item.name, bool(value))
                elif isinstance(current, str):
                    setattr(spec, item.name, str(value))
                else:
                    setattr(spec, item.name, float(value))
            except (TypeError, ValueError):
                pass
    return PrintSettings(printer=str(raw.get("printer") or ""), label=spec,
                         last_file=str(raw.get("last_file") or ""))


def save(settings: PrintSettings) -> None:
    payload = {"printer": settings.printer, "label": asdict(settings.spec()),
               "last_file": settings.last_file}
    target = path()
    if directory := os.path.dirname(target):
        os.makedirs(directory, exist_ok=True)
    temporary = f"{target}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, target)
