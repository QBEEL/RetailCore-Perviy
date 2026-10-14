"""Запись в журнал изменений: что именно изменилось и о какой записи речь.

Журнал отвечает на вопрос «кто что делал», а номер оплаты на него не отвечает:
через неделю по «создание 22122» не понять, что это за оплата, а удалённую не
найти нигде. Поэтому вместе с действием хранится краткий снимок самой записи
(получатель, сумма, дата, документ), а при правке — только то, что поменялось,
вместе с прежним значением.

Модуль не ходит в базу, пока не вызвана `record`, — так его проверяют без сервера.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping

# Ключ снимка записи внутри `changes`. Клиент журнала переносит его в колонку
# «Запись» и в самих изменениях не показывает.
ABOUT = "about"
# Ключ прежних значений изменённых полей.
WAS = "was"

# Поля оплаты, по которым строится снимок.
ABOUT_FIELDS = ("recipient", "amount", "pay_date", "doc_number")

# Числа сравниваются с точностью до копейки: Decimal из базы и float из запроса
# иначе расходились бы в шестом знаке и давали «изменение», которого не было.
_EPSILON = 0.005


def _plain(value: Any) -> Any:
    """Значение в виде, пригодном для JSON и для сравнения."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def same(old: Any, new: Any) -> bool:
    """Равны ли значения поля. Пустая строка и отсутствие значения — одно и то же."""
    old, new = _plain(old), _plain(new)
    if old in (None, "") and new in (None, ""):
        return True
    if isinstance(old, (int, float)) and isinstance(new, (int, float)) \
            and not isinstance(old, bool) and not isinstance(new, bool):
        return abs(old - new) < _EPSILON
    return old == new


def diff(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """Что изменилось: новые значения и под ключом `was` — прежние.

    Поле, которого в `new` нет, не меняется. Если не изменилось ничего, словарь
    пуст — писать в журнал тогда нечего.
    """
    now: dict[str, Any] = {}
    was: dict[str, Any] = {}
    for name, value in new.items():
        before = old.get(name)
        if same(before, value):
            continue
        now[name] = _plain(value)
        was[name] = _plain(before)
    if not now:
        return {}
    now[WAS] = was
    return now


def snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    """Краткая выписка об оплате для журнала. Пустые поля не берутся."""
    about = {name: _plain(row.get(name)) for name in ABOUT_FIELDS}
    return {name: value for name, value in about.items() if value not in (None, "")}


def money(amount: Any) -> str:
    """Сумма с пробелами между разрядами: «100 000 ₽»."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return ""
    text = f"{value:,.2f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"
    return text.replace(",", " ").replace(".", ",") + " ₽"


def _day(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return ""


def describe(about: Mapping[str, Any]) -> str:
    """Оплата словами: «Получатель · 100 000 ₽ · 14.10.2026 · док. 123»."""
    parts = [
        str(about.get("recipient") or ""),
        money(about["amount"]) if about.get("amount") is not None else "",
        _day(about["pay_date"]) if about.get("pay_date") else "",
        f"док. {about['doc_number']}" if about.get("doc_number") else "",
    ]
    return " · ".join(part for part in parts if part)


def record(user_id: int, entity: str, entity_id: int, action: str,
           changes: Mapping[str, Any] | None = None) -> None:
    """Пишет строку журнала."""
    from . import db

    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action, changes)"
        " VALUES (%s, %s, %s, %s, %s)",
        (user_id, entity, entity_id, action,
         json.dumps(changes or {}, default=str, ensure_ascii=False)))
