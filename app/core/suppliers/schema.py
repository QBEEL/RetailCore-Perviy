"""Схема базы поставщиков и её миграции.

Устроена так же, как база снимков: версия лежит во встроенном счётчике
`PRAGMA user_version`, каждый шаг миграции — отдельный элемент `_MIGRATIONS`,
недостающие применяются сами при обновлении приложения.
"""
from __future__ import annotations

import sqlite3

_V1 = """
CREATE TABLE IF NOT EXISTS supplier (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    key        TEXT    NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    brands     TEXT    NOT NULL DEFAULT '',
    categories TEXT    NOT NULL DEFAULT '',
    contact    TEXT    NOT NULL DEFAULT '',
    note       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

-- Поставщик заводится один раз: повторное имя должно попадать в ту же карточку.
CREATE UNIQUE INDEX IF NOT EXISTS supplier_key ON supplier(key);

-- Как узнать поставщика по имени файла: «zielinski», «зелински», «ИП Саух».
CREATE TABLE IF NOT EXISTS supplier_alias (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES supplier(id) ON DELETE CASCADE,
    pattern     TEXT    NOT NULL,
    key         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS supplier_alias_owner ON supplier_alias(supplier_id);
CREATE UNIQUE INDEX IF NOT EXISTS supplier_alias_key ON supplier_alias(supplier_id, key);

CREATE TABLE IF NOT EXISTS supplier_layout (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id         INTEGER NOT NULL REFERENCES supplier(id) ON DELETE CASCADE,
    sheet_name          TEXT    NOT NULL DEFAULT '',
    signature           TEXT    NOT NULL DEFAULT '',
    titles              TEXT    NOT NULL DEFAULT '[]',
    price_map           TEXT    NOT NULL DEFAULT '{}',
    role_map            TEXT    NOT NULL DEFAULT '{}',
    separators          TEXT    NOT NULL DEFAULT '',
    modifier_separators TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL,
    last_used_at        TEXT    NOT NULL DEFAULT '',
    uses                INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS supplier_layout_owner ON supplier_layout(supplier_id);
-- Прайс той же структуры должен находить свою запись, а не создавать новую.
CREATE UNIQUE INDEX IF NOT EXISTS supplier_layout_shape
    ON supplier_layout(supplier_id, signature, sheet_name);

CREATE TABLE IF NOT EXISTS supplier_link (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id      INTEGER NOT NULL REFERENCES supplier(id) ON DELETE CASCADE,
    identity         TEXT    NOT NULL,
    nomenclature     TEXT    NOT NULL DEFAULT '',
    characteristic   TEXT    NOT NULL DEFAULT '',
    article_key      TEXT    NOT NULL DEFAULT '',
    volume           TEXT    NOT NULL DEFAULT '',
    name_key         TEXT    NOT NULL DEFAULT '',
    onec_article     TEXT    NOT NULL DEFAULT '',
    onec_name        TEXT    NOT NULL DEFAULT '',
    supplier_article TEXT    NOT NULL DEFAULT '',
    supplier_sku     TEXT    NOT NULL DEFAULT '',
    supplier_ean     TEXT    NOT NULL DEFAULT '',
    supplier_name    TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL,
    author           TEXT    NOT NULL DEFAULT '',
    note             TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS supplier_link_owner ON supplier_link(supplier_id);
-- Повторная привязка той же строки заменяет прежнюю, а не копится рядом.
CREATE UNIQUE INDEX IF NOT EXISTS supplier_link_identity
    ON supplier_link(supplier_id, identity);
"""


# Отсрочка платежа: сколько дней проходит от заявки до оплаты. Ноль означает
# «считать по истории оплат» — у большинства поставщиков она устойчива, и
# посчитанная медиана точнее введённой на память.
_V2 = """
ALTER TABLE supplier ADD COLUMN payment_terms_days INTEGER NOT NULL DEFAULT 0;
"""


def _step_1(connection: sqlite3.Connection) -> None:
    connection.executescript(_V1)


def _step_2(connection: sqlite3.Connection) -> None:
    connection.executescript(_V2)


_MIGRATIONS = (_step_1, _step_2)
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
