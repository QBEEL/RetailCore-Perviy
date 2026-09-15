"""Откуда брать оплаты: из общей базы на сервере или из своей локальной.

Интерфейс обращается сюда вместо `store`, и один и тот же код работает в обоих
случаях. Выбор делается на каждом вызове, а не при импорте: вход выполняется
уже после того, как окно построено, и запомненный при загрузке модуля источник
остался бы локальным навсегда.

Переадресация опирается на PEP 562: обращение к неизвестному имени модуля
попадает в `__getattr__`, откуда уходит в выбранный источник. Поэтому здесь
намеренно нет ни одного публичного имени — любое совпадение перехватило бы
вызов раньше, чем сработает выбор.
"""
from __future__ import annotations

from typing import Any

from . import remote as _remote
from . import store as _store
from . import transport as _transport

__all__ = ["backend", "local_only", "online", "set_local_only"]

# Работать намеренно на своей базе, не выходя из учётной записи. Нужно
# администратору: выгрузку 1С и присланные планы он сначала прогоняет у себя,
# смотрит, что вышло, и только потом отдаёт отделу. Вход при этом сохраняется —
# поставщики, закрепления и личный кабинет остаются на месте.
_local_only = False


def local_only() -> bool:
    """Выбрана ли своя база при действующем входе."""
    return _local_only


def set_local_only(value: bool) -> None:
    global _local_only
    _local_only = bool(value)


def online() -> bool:
    """Работаем ли с общей базой."""
    return _transport.session.active and not _local_only


def backend() -> Any:
    """Действующий источник данных."""
    return _remote if online() else _store


def __getattr__(name: str) -> Any:
    return getattr(backend(), name)


def __dir__() -> list[str]:
    return sorted({*globals(), *dir(backend())})
