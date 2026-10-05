"""Макет этикетки с кодом маркировки и запоминание настроек печати.

Размеры по умолчанию — те, которыми отдел печатает в ПРИНТМАРКИ (файл шаблона
«43x25»): этикетка 43×25 мм, DataMatrix 15×15 мм слева сверху, справа название
товара, под символом — GTIN и серийный номер. Один и тот же макет не должен
менять вид от программы к программе, иначе этикетки на одной полке выглядят
по-разному.

Настройки хранятся отдельно от `settings.json`: это не настройки окна, а то,
как настроен принтер конкретного рабочего места, и копировать их вместе с
остальным не нужно.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields

from .. import appdata

FILE_NAME = "marking_print.json"


@dataclass(slots=True)
class LabelSpec:
    """Размеры в миллиметрах, шрифты в пунктах."""

    width: float = 43.0
    height: float = 25.0
    # Символ DataMatrix. Сторона 15 мм при 203 dpi даёт 120 точек — на
    # символ 32×32 – 36×36 модулей приходится по 3 точки, и сканер его читает.
    dm_size: float = 15.0
    dm_left: float = 2.0
    dm_top: float = 1.0
    # Название справа от символа.
    show_name: bool = True
    name_left: float = 18.0
    name_top: float = 1.0
    name_width: float = 24.0
    name_height: float = 15.0
    name_font: float = 5.0
    # GTIN и серийный номер под символом.
    show_text: bool = True
    text_left: float = 1.0
    text_width: float = 41.0
    text_font: float = 5.0
    # Подгонка под принтер: многие сдвигают печать на долю миллиметра.
    offset_x: float = 0.0
    offset_y: float = 0.0

    @property
    def text_top(self) -> float:
        return self.dm_top + self.dm_size + 0.6

    @property
    def problems(self) -> list[str]:
        found: list[str] = []
        if self.width < 10 or self.height < 10:
            found.append("размер этикетки меньше 10 мм")
        if self.dm_size < 8:
            found.append("DataMatrix меньше 8 мм сканер читает плохо")
        if self.dm_left + self.dm_size + self.offset_x > self.width + 0.01:
            found.append("символ не помещается по ширине этикетки")
        if self.dm_top + self.dm_size + self.offset_y > self.height + 0.01:
            found.append("символ не помещается по высоте этикетки")
        return found


@dataclass(slots=True)
class PrintSettings:
    """Что выбрано для печати на этом рабочем месте."""

    printer: str = ""
    label: LabelSpec | None = None
    # Какой документ ввода в оборот подаёт это рабочее место и с какой причиной —
    # чтобы не выбирать одно и то же при каждой отправке.
    introduce_kind: str = "remark"
    remark_cause: str = "KM_SPOILED"

    def spec(self) -> LabelSpec:
        return self.label or LabelSpec()


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
    spec = LabelSpec()
    saved = raw.get("label")
    if isinstance(saved, dict):
        for item in fields(LabelSpec):
            if item.name in saved:
                try:
                    value = saved[item.name]
                    setattr(spec, item.name,
                            bool(value) if isinstance(getattr(spec, item.name), bool)
                            else float(value))
                except (TypeError, ValueError):
                    pass
    return PrintSettings(printer=str(raw.get("printer") or ""), label=spec,
                         introduce_kind=str(raw.get("introduce_kind") or "remark"),
                         remark_cause=str(raw.get("remark_cause") or "KM_SPOILED"))


def save(settings: PrintSettings) -> None:
    payload = {"printer": settings.printer, "label": asdict(settings.spec()),
               "introduce_kind": settings.introduce_kind,
               "remark_cause": settings.remark_cause}
    target = path()
    if directory := os.path.dirname(target):
        os.makedirs(directory, exist_ok=True)
    temporary = f"{target}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, target)
