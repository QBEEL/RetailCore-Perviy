"""Схема базы заметок и её миграции.

Устроена так же, как база поставщиков: версия лежит во встроенном счётчике
`PRAGMA user_version`, каждый шаг миграции — отдельный элемент `_MIGRATIONS`,
недостающие применяются сами при обновлении приложения.
"""
from __future__ import annotations

import sqlite3

_V1 = """
CREATE TABLE IF NOT EXISTS note (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NULL — страница верхнего уровня. Вложенные удаляются вместе с родителем.
    parent_id  INTEGER REFERENCES note(id) ON DELETE CASCADE,
    title      TEXT    NOT NULL DEFAULT '',
    body       TEXT    NOT NULL DEFAULT '',
    position   INTEGER NOT NULL DEFAULT 0,
    -- Заголовок и текст в нормализованном виде (регистр, ё→е): SQLite LIKE
    -- не различает регистр у кириллицы, поэтому ищем по этому полю в коде.
    search_key TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS note_parent ON note(parent_id, position);
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
