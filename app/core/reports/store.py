"""Локальное хранилище профилей отчётов и правил объединения магазинов.

Соединение SQLite нельзя делить между потоками, поэтому каждая функция
открывает своё и закрывает за собой — так же, как в базах оплат и поставщиков:
чтение идёт из потока интерфейса, а построение отчёта уходит в фоновую задачу.

Набор функций повторяет `remote`: интерфейс не должен знать, отвечает ему
сервер или своя база.
"""
from __future__ import annotations

import getpass
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Sequence

from .. import appdata
from . import schema
from .models import ReportProfile, StoreRule, default_profile
from .stores import normalize

DB_FILE = "reports.db"


def database_path() -> str:
    return appdata.path_to(DB_FILE)


@contextmanager
def connect(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Открывает базу, при необходимости создавая её и приводя схему к версии."""
    target = path or database_path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        schema.migrate(connection)
        yield connection
    finally:
        connection.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _author() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — имя пользователя не должно ломать запись
        return ""


# --- профили --------------------------------------------------------------------

def list_profiles(path: str | None = None) -> list[ReportProfile]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT id, name, supplier, supplier_id, payload, updated_at, updated_by"
            " FROM report_profile ORDER BY name").fetchall()
    return [_profile(row) for row in rows]


def _profile(row: sqlite3.Row) -> ReportProfile:
    try:
        payload = json.loads(row["payload"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    payload.update({"name": row["name"], "supplier": row["supplier"],
                    "supplier_id": row["supplier_id"]})
    profile = ReportProfile.from_dict(payload)
    profile.id = int(row["id"])
    profile.updated_by = row["updated_by"] or ""
    profile.updated_at = _moment(row["updated_at"])
    return profile


def _moment(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def save_profile(profile: ReportProfile, path: str | None = None) -> ReportProfile:
    """Создаёт или обновляет профиль. Имя уникально — по нему его и узнают."""
    if not profile.name.strip():
        raise ValueError("У профиля отчёта должно быть название")
    payload = json.dumps(profile.as_dict(), ensure_ascii=False)
    author, now = _author(), _now()
    with connect(path) as connection:
        if profile.id:
            connection.execute(
                "UPDATE report_profile SET name = ?, supplier = ?, supplier_id = ?,"
                " payload = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                (profile.name, profile.supplier, profile.supplier_id, payload,
                 now, author, profile.id))
        else:
            cursor = connection.execute(
                "INSERT INTO report_profile (name, supplier, supplier_id, payload,"
                "                            updated_at, updated_by)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (profile.name, profile.supplier, profile.supplier_id, payload,
                 now, author))
            profile.id = int(cursor.lastrowid)
        connection.commit()
    profile.updated_at = _moment(now)
    profile.updated_by = author
    return profile


def delete_profile(profile_id: int, path: str | None = None) -> None:
    with connect(path) as connection:
        connection.execute("DELETE FROM report_profile WHERE id = ?", (profile_id,))
        connection.commit()


def ensure_default(path: str | None = None) -> list[ReportProfile]:
    """Первый запуск не должен встречать пустым списком.

    Профиль повторяет отчёт, который собирался руками, поэтому первую выгрузку
    можно сделать сразу и сверить с прошлым месяцем.
    """
    profiles = list_profiles(path)
    if profiles:
        return profiles
    profile = default_profile("SmartBeauty")
    profile.name = "SmartBeauty — акции"
    profile.signatures = ["Категорийный менеджер ______________ /______________/"]
    save_profile(profile, path)
    return list_profiles(path)


# --- правила магазинов -----------------------------------------------------------

def list_rules(path: str | None = None) -> list[StoreRule]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT id, source, target, enabled, comment, updated_at, updated_by"
            " FROM store_rule ORDER BY source").fetchall()
    return [_rule(row) for row in rows]


def _rule(row: sqlite3.Row) -> StoreRule:
    return StoreRule(
        id=int(row["id"]),
        source=row["source"] or "",
        target=row["target"] or "",
        enabled=bool(row["enabled"]),
        comment=row["comment"] or "",
        updated_at=_moment(row["updated_at"]),
        updated_by=row["updated_by"] or "",
    )


def save_rule(rule: StoreRule, path: str | None = None) -> StoreRule:
    """Записывает правило. Источник уникален: повторный вызов переписывает цель."""
    if not rule.valid:
        raise ValueError("В правиле должны быть указаны и источник, и приёмник")
    if normalize(rule.source) == normalize(rule.target):
        raise ValueError("Магазин нельзя объединить сам с собой")
    author, now = _author(), _now()
    with connect(path) as connection:
        connection.execute(
            "INSERT INTO store_rule (source_key, source, target, enabled, comment,"
            "                        updated_at, updated_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source_key) DO UPDATE SET"
            "   source = excluded.source, target = excluded.target,"
            "   enabled = excluded.enabled, comment = excluded.comment,"
            "   updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (normalize(rule.source), rule.source.strip(), rule.target.strip(),
             int(rule.enabled), rule.comment, now, author))
        connection.commit()
        row = connection.execute(
            "SELECT id, source, target, enabled, comment, updated_at, updated_by"
            " FROM store_rule WHERE source_key = ?",
            (normalize(rule.source),)).fetchone()
    return _rule(row)


def delete_rule(rule_id: int, path: str | None = None) -> None:
    with connect(path) as connection:
        connection.execute("DELETE FROM store_rule WHERE id = ?", (rule_id,))
        connection.commit()


def replace_rules(rules: Sequence[StoreRule], path: str | None = None) -> None:
    """Полная замена набора правил — для приёма списка с сервера в локальный кэш."""
    author, now = _author(), _now()
    with connect(path) as connection:
        connection.execute("DELETE FROM store_rule")
        connection.executemany(
            "INSERT OR REPLACE INTO store_rule (source_key, source, target, enabled,"
            "                                   comment, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(normalize(rule.source), rule.source.strip(), rule.target.strip(),
              int(rule.enabled), rule.comment,
              rule.updated_at.isoformat(timespec="seconds") if rule.updated_at else now,
              rule.updated_by or author)
             for rule in rules if rule.valid])
        connection.commit()
