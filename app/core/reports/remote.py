"""Профили отчётов и правила магазинов с сервера. Замена `store`.

Имена функций, аргументы и типы возвращаемых значений повторяют `store`, чтобы
интерфейс не заметил подмены — так же, как это сделано в оплатах.

Ради этого сохранён и аргумент `path`: локальному хранилищу он говорит, какую
базу открыть, здесь не значит ничего, но убирать его пришлось бы вместе с
правкой всех вызовов.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from ..payments import transport
from ..payments.transport import ServerError
from .models import ReportProfile, StoreRule

PROFILES = "/api/reports/profiles"
RULES = "/api/reports/store-rules"


def _ask(method, *args):
    """Запрос к серверу с понятным ответом на «раздела там ещё нет».

    Отчётность появилась позже оплат, и сервер обновляют отдельно от
    приложения. Пока миграция не применена, все эти адреса отвечают 404, а
    «Not Found» посреди работы не говорит пользователю ничего — он начнёт
    искать причину у себя.
    """
    try:
        return method(*args)
    except ServerError as error:
        if error.status == 404:
            raise ServerError(
                "Общая база ещё не знает про отчётность: сервер обновлён не до "
                "конца.\n\nПередайте это администратору — нужно применить "
                "миграцию 003_reports.sql и перезапустить API. Пока этого не "
                "произошло, отчёты можно собирать, выйдя из общей базы: "
                "профили и правила возьмутся из своей.", 404) from None
        raise


def _moment(value: Any) -> datetime | None:
    try:
        # Сервер отдаёт время с зоной; приложение показывает местное и о зонах
        # ничего не знает, поэтому она отбрасывается после перевода.
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


# --- профили --------------------------------------------------------------------

def _profile(data: dict) -> ReportProfile:
    profile = ReportProfile.from_dict(data.get("payload") or {})
    profile.id = int(data.get("id") or 0)
    profile.name = str(data.get("name") or profile.name)
    profile.supplier = str(data.get("supplier") or profile.supplier)
    profile.supplier_id = int(data.get("supplier_id") or profile.supplier_id)
    profile.updated_at = _moment(data.get("updated_at"))
    profile.updated_by = str(data.get("updated_by") or "")
    return profile


def list_profiles(path: str | None = None) -> list[ReportProfile]:
    return [_profile(item) for item in _ask(transport.get, PROFILES) or []]


def save_profile(profile: ReportProfile, path: str | None = None) -> ReportProfile:
    if not profile.name.strip():
        raise ValueError("У профиля отчёта должно быть название")
    body = {"name": profile.name, "supplier": profile.supplier,
            "supplier_id": profile.supplier_id, "payload": profile.as_dict()}
    if profile.id:
        return _profile(_ask(transport.patch, f"{PROFILES}/{profile.id}", body))
    return _profile(_ask(transport.post, PROFILES, body))


def delete_profile(profile_id: int, path: str | None = None) -> None:
    _ask(transport.delete, f"{PROFILES}/{profile_id}")


def ensure_default(path: str | None = None) -> list[ReportProfile]:
    """На сервере профили заводит администратор.

    Молча создавать общий профиль от имени того, кто просто открыл вкладку,
    нельзя: он появится у всего отдела и будет выглядеть как чужая настройка.
    """
    return list_profiles(path)


# --- правила магазинов -----------------------------------------------------------

def _rule(data: dict) -> StoreRule:
    return StoreRule(
        id=int(data.get("id") or 0),
        source=str(data.get("source") or ""),
        target=str(data.get("target") or ""),
        enabled=bool(data.get("enabled", True)),
        comment=str(data.get("comment") or ""),
        updated_at=_moment(data.get("updated_at")),
        updated_by=str(data.get("updated_by") or ""),
    )


def list_rules(path: str | None = None) -> list[StoreRule]:
    return [_rule(item) for item in _ask(transport.get, RULES) or []]


def save_rule(rule: StoreRule, path: str | None = None) -> StoreRule:
    if not rule.valid:
        raise ValueError("В правиле должны быть указаны и источник, и приёмник")
    return _rule(_ask(transport.put, RULES, rule.as_dict()))


def delete_rule(rule_id: int, path: str | None = None) -> None:
    _ask(transport.delete, f"{RULES}/{rule_id}")


def replace_rules(rules: Sequence[StoreRule], path: str | None = None) -> None:
    """На сервере набор целиком не заменяется.

    Правила общие, и «заменить все» одним нажатием — это чужие настройки,
    стёртые без спроса. Каждое правится по отдельности.
    """
    raise NotImplementedError(
        "Правила в общей базе меняются по одному, а не набором")
