"""Схема базы оплат и её миграции.

Устроена как база поставщиков: версия в `PRAGMA user_version`, каждый шаг —
отдельный элемент `_MIGRATIONS`, недостающие применяются сами при обновлении.

База отдельная от `suppliers.db` намеренно: у оплат свой объём (тысячи строк
против сотен), свой ритм изменений и своя история миграций. Ссылка на карточку
поставщика хранится числом без внешнего ключа — базы разные, и целостность
проверяется при чтении.
"""
from __future__ import annotations

import sqlite3

_V1 = """
CREATE TABLE IF NOT EXISTS payment (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_number    TEXT    NOT NULL DEFAULT '',
    request_date  TEXT    NOT NULL DEFAULT '',
    pay_date      TEXT    NOT NULL DEFAULT '',
    amount        REAL    NOT NULL DEFAULT 0,
    vat           REAL    NOT NULL DEFAULT 0,
    currency      TEXT    NOT NULL DEFAULT 'руб.',
    supplier_id   INTEGER NOT NULL DEFAULT 0,
    recipient     TEXT    NOT NULL DEFAULT '',
    recipient_key TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'planned',
    source_status TEXT    NOT NULL DEFAULT '',
    paid_flag     INTEGER NOT NULL DEFAULT 0,
    operation     TEXT    NOT NULL DEFAULT '',
    over_limit    INTEGER NOT NULL DEFAULT 0,
    priority      TEXT    NOT NULL DEFAULT '',
    edo_state     TEXT    NOT NULL DEFAULT '',
    responsible   TEXT    NOT NULL DEFAULT '',
    author        TEXT    NOT NULL DEFAULT '',
    comment       TEXT    NOT NULL DEFAULT '',
    had_files     INTEGER NOT NULL DEFAULT 0,
    origin        TEXT    NOT NULL DEFAULT 'manual',
    origin_ref    TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

-- Номер заявки 1С обнуляется каждый год: IP00-000001 встречается и в 2022,
-- и в 2026. Уникальна пара с датой заявки — на 6929 строках выгрузки она
-- не дала ни одной коллизии. Ключ частичный: у записей, созданных в
-- приложении, номера нет, и пустые пары не должны конфликтовать между собой.
CREATE UNIQUE INDEX IF NOT EXISTS payment_doc
    ON payment(doc_number, request_date)
    WHERE doc_number <> '';

CREATE INDEX IF NOT EXISTS payment_pay_date ON payment(pay_date);
CREATE INDEX IF NOT EXISTS payment_status ON payment(status);
CREATE INDEX IF NOT EXISTS payment_supplier ON payment(supplier_id, pay_date);
CREATE INDEX IF NOT EXISTS payment_recipient ON payment(recipient_key, pay_date);

CREATE TABLE IF NOT EXISTS payment_file (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL REFERENCES payment(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL DEFAULT '',
    path       TEXT    NOT NULL DEFAULT '',
    size       INTEGER NOT NULL DEFAULT 0,
    added_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS payment_file_owner ON payment_file(payment_id);

CREATE TABLE IF NOT EXISTS budget (
    year       INTEGER NOT NULL,
    month      INTEGER NOT NULL,
    amount     REAL    NOT NULL DEFAULT 0,
    note       TEXT    NOT NULL DEFAULT '',
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (year, month)
);

-- «Получатель» в 1С — это юрлицо («НеваЛайн ООО»), а карточка поставщика
-- заведена под торговым именем. Соответствие ищется по нормализованному имени
-- и запоминается здесь, чтобы автоподбор не повторялся на каждом чтении.
CREATE TABLE IF NOT EXISTS recipient_link (
    recipient_key TEXT    PRIMARY KEY,
    recipient     TEXT    NOT NULL DEFAULT '',
    supplier_id   INTEGER NOT NULL DEFAULT 0,
    linked_by     TEXT    NOT NULL DEFAULT 'auto',
    updated_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS recipient_link_supplier ON recipient_link(supplier_id);

CREATE TABLE IF NOT EXISTS import_run (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT    NOT NULL DEFAULT '',
    file_hash    TEXT    NOT NULL DEFAULT '',
    rows_total   INTEGER NOT NULL DEFAULT 0,
    rows_new     INTEGER NOT NULL DEFAULT 0,
    rows_updated INTEGER NOT NULL DEFAULT 0,
    rows_same    INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    error        TEXT    NOT NULL DEFAULT '',
    finished_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS import_run_hash ON import_run(file_hash);
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
