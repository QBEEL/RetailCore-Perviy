"""Схема базы снимков и её миграции.

Версия схемы хранится во встроенном счётчике SQLite `PRAGMA user_version`,
поэтому служебная таблица не нужна. Каждый шаг миграции — отдельный элемент
`_MIGRATIONS`; при обновлении приложения недостающие шаги применяются сами.
"""
from __future__ import annotations

import sqlite3

_V1 = """
CREATE TABLE IF NOT EXISTS snapshot (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT    NOT NULL,
    source_file_name  TEXT    NOT NULL,
    source_file_path  TEXT    NOT NULL,
    source_file_hash  TEXT    NOT NULL,
    sheet_name        TEXT    NOT NULL DEFAULT '',
    total_products    INTEGER NOT NULL DEFAULT 0,
    brand             TEXT    NOT NULL DEFAULT '',
    category          TEXT    NOT NULL DEFAULT '',
    user_id           TEXT    NOT NULL DEFAULT '',
    description       TEXT    NOT NULL DEFAULT ''
);

-- Повторная загрузка того же содержимого не создаёт вторую версию.
CREATE UNIQUE INDEX IF NOT EXISTS snapshot_content
    ON snapshot(source_file_hash, sheet_name);

CREATE TABLE IF NOT EXISTS product (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    row         INTEGER NOT NULL,
    article     TEXT NOT NULL DEFAULT '',
    ean         TEXT NOT NULL DEFAULT '',
    sku         TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    brand       TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT '',
    volume      TEXT NOT NULL DEFAULT '',
    price       REAL,
    match_key   TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS product_snapshot ON product(snapshot_id);
CREATE INDEX IF NOT EXISTS product_article  ON product(snapshot_id, article);
CREATE INDEX IF NOT EXISTS product_ean      ON product(snapshot_id, ean);
"""

# Индекс списка — история открывается сортированной по дате.
_V2 = """
CREATE INDEX IF NOT EXISTS snapshot_created ON snapshot(created_at DESC);
"""

# Разметка колонок входит в тождество снимка: у одного и того же файла цена
# может читаться из разных колонок (доллары или рубли за разный объём заказа),
# и после смены колонки в настройках нужен новый снимок, а не отказ по дублю.
_V3 = """
DROP INDEX IF EXISTS snapshot_content;
CREATE UNIQUE INDEX snapshot_content
    ON snapshot(source_file_hash, sheet_name, layout);
"""


def _step_1(connection: sqlite3.Connection) -> None:
    connection.executescript(_V1)


def _step_2(connection: sqlite3.Connection) -> None:
    connection.executescript(_V2)


def _step_3(connection: sqlite3.Connection) -> None:
    _add_column(connection, "snapshot", "layout", "TEXT NOT NULL DEFAULT ''")
    connection.executescript(_V3)


_MIGRATIONS = (_step_1, _step_2, _step_3)
VERSION = len(_MIGRATIONS)


def _add_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Добавляет колонку, если её ещё нет.

    В SQLite у ALTER TABLE нет IF NOT EXISTS, а миграция может оказаться
    применённой наполовину — например, если базу успел открыть exe более
    старой версии. Без этой проверки повторный проход падал бы с
    «duplicate column name».
    """
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate(connection: sqlite3.Connection) -> int:
    """Доводит схему до актуальной версии. Повторный вызов ничего не меняет."""
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    if current >= VERSION:
        # База сделана более новой версией приложения. Понижать номер нельзя:
        # тогда та версия применила бы свои миграции заново, поверх готовой
        # схемы. Работаем с тем, что есть, — колонки нужных нам версий на месте.
        return current

    for step in range(current, VERSION):
        _MIGRATIONS[step](connection)
    # Параметры в PRAGMA не подставляются — значение только из своего кода.
    connection.execute(f"PRAGMA user_version = {VERSION}")
    connection.commit()
    return VERSION
