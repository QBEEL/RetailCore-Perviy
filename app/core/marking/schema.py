"""Схема базы маркировки и её миграции.

Устроена как базы оплат и отчётности: версия в `PRAGMA user_version`, каждый
шаг — отдельный элемент `_MIGRATIONS`, недостающие применяются сами.

База локальная и общей не станет: операция подписывается личным сертификатом
конкретного человека, и «общий журнал операций» означал бы, что видно, кто чем
подписался. Обмен здесь идёт через саму ГИС МТ, а не через наш сервер.
"""
from __future__ import annotations

import sqlite3

_V1 = """
-- Операция с кодами. Живёт дольше запроса намеренно: ГИС МТ отвечает
-- «принято», а результат выясняется опросом, и операция, отправленная перед
-- закрытием приложения, обязана дождаться ответа после запуска.
CREATE TABLE IF NOT EXISTS operation (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    local_id      TEXT    NOT NULL,
    kind          TEXT    NOT NULL DEFAULT 'check',
    product_group TEXT    NOT NULL DEFAULT '',
    contour       TEXT    NOT NULL DEFAULT 'sandbox',
    status        TEXT    NOT NULL DEFAULT 'draft',
    document_id   TEXT    NOT NULL DEFAULT '',
    -- Коды одной строкой через перевод строки: их читают и пишут только
    -- целиком, отдельная таблица дала бы соединение на каждом чтении и ничего
    -- взамен.
    codes         TEXT    NOT NULL DEFAULT '',
    -- Отпечаток набора кодов. По нему ловится повторный запуск той же
    -- операции — второй вывод из оборота одних и тех же кодов.
    codes_hash    TEXT    NOT NULL DEFAULT '',
    reason        TEXT    NOT NULL DEFAULT '',
    comment       TEXT    NOT NULL DEFAULT '',
    error         TEXT    NOT NULL DEFAULT '',
    author        TEXT    NOT NULL DEFAULT '',
    certificate   TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    sent_at       TEXT    NOT NULL DEFAULT '',
    checked_at    TEXT    NOT NULL DEFAULT ''
);

-- Свой идентификатор уникален: он присваивается до отправки и служит защитой
-- от того, что операция заведётся дважды из-за повторного нажатия.
CREATE UNIQUE INDEX IF NOT EXISTS operation_local ON operation(local_id);

CREATE INDEX IF NOT EXISTS operation_status ON operation(status, created_at DESC);
CREATE INDEX IF NOT EXISTS operation_hash ON operation(codes_hash, kind);
CREATE INDEX IF NOT EXISTS operation_document ON operation(document_id)
    WHERE document_id <> '';

-- Что ГИС МТ ответила про конкретный код. Хранится, чтобы приёмка на тысячу
-- позиций не спрашивала одно и то же по второму разу и работала, когда сеть
-- отвалилась посреди сверки.
CREATE TABLE IF NOT EXISTS code_check (
    code         TEXT    PRIMARY KEY,
    gtin         TEXT    NOT NULL DEFAULT '',
    serial       TEXT    NOT NULL DEFAULT '',
    state        TEXT    NOT NULL DEFAULT 'unknown',
    valid        INTEGER NOT NULL DEFAULT 0,
    found        INTEGER NOT NULL DEFAULT 0,
    owner_inn    TEXT    NOT NULL DEFAULT '',
    owner_name   TEXT    NOT NULL DEFAULT '',
    product_name TEXT    NOT NULL DEFAULT '',
    contour      TEXT    NOT NULL DEFAULT 'sandbox',
    checked_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS code_check_gtin ON code_check(gtin);
CREATE INDEX IF NOT EXISTS code_check_moment ON code_check(checked_at);
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
