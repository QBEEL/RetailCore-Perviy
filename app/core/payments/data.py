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

__all__ = ["backend", "online"]


def online() -> bool:
    """Работаем ли с общей базой."""
    return _transport.session.active


def backend() -> Any:
    """Действующий источник данных."""
    return _remote if online() else _store


def __getattr__(name: str) -> Any:
    return getattr(backend(), name)


def __dir__() -> list[str]:
    return sorted({*globals(), *dir(backend())})
