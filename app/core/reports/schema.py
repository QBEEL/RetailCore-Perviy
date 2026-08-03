"""Схема базы отчётности и её миграции.

Устроена как база оплат: версия в `PRAGMA user_version`, каждый шаг — отдельный
элемент `_MIGRATIONS`, недостающие применяются сами при обновлении.

Локальная база здесь — не основное хранилище, а запасное: правила и профили
живут на сервере и общие для всех менеджеров. Своя копия нужна тем, кто ещё не
подключён к общей базе, и всем остальным — когда сервер недоступен.
"""
from __future__ import annotations

import sqlite3

_V1 = """
-- Формат отчёта для одного поставщика. Всё, кроме имени и поставщика, лежит
-- одним документом: набор полей, метрик и фильтров меняется вместе, читается
-- целиком и порознь никогда не запрашивается.
CREATE TABLE IF NOT EXISTS report_profile (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL DEFAULT '',
    supplier    TEXT    NOT NULL DEFAULT '',
    supplier_id INTEGER NOT NULL DEFAULT 0,
    payload     TEXT    NOT NULL DEFAULT '{}',
    updated_at  TEXT    NOT NULL,
    updated_by  TEXT    NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS report_profile_name ON report_profile(name);

-- Правило «продажи источника учитывать за приёмником». Источник уникален:
-- отправить один магазин сразу в два — это молча удвоенные продажи, и такую
-- настройку лучше не дать сделать, чем потом искать расхождение в отчёте.
CREATE TABLE IF NOT EXISTS store_rule (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT    NOT NULL,
    source     TEXT    NOT NULL DEFAULT '',
    target     TEXT    NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    comment    TEXT    NOT NULL DEFAULT '',
    updated_at TEXT    NOT NULL,
    updated_by TEXT    NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS store_rule_source ON store_rule(source_key);
"""


def _step_1(connection: sqlite3.Connection) -> None:
    connection.executescript(_V1)


_MIGRATIONS = (_step_1,)
VERSION = len(_MIGRATIONS)


def migrate(connection: sqlite3.Connection) -> int:
    """Доводит схему до актуальной версии. Повторный вызов ничего не меняет."""
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    if current >= VERSION:
        # База сделана более новой версией приложения. Понижать номер нельзя:
        # та версия применила бы свои миграции заново поверх готовой схемы.
        return current
    for step in range(current, VERSION):
        _MIGRATIONS[step](connection)
    # Параметры в PRAGMA не подставляются — значение только из своего кода.
    connection.execute(f"PRAGMA user_version = {VERSION}")
    connection.commit()
    return VERSION
