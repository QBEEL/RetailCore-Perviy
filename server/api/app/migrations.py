"""Транзакционные миграции PostgreSQL; запускаются отдельно от API."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.rows import dict_row

_PACKAGED_DB = Path(__file__).resolve().parents[1] / "db"
DB_DIR = _PACKAGED_DB if _PACKAGED_DB.is_dir() else Path(__file__).resolve().parents[2] / "db"
LOCK_ID = 0x5243544D494752
BASELINE = "001_schema.sql"


class MigrationError(RuntimeError):
    """История миграций не соответствует поставляемой схеме."""


@dataclass(frozen=True)
class Migration:
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        # read_text нормализует CRLF: checkout в Windows и Linux даёт один хеш.
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def load(db_dir: Path = DB_DIR) -> list[Migration]:
    baseline = db_dir / "schema.sql"
    files = sorted((db_dir / "migrations").glob("*.sql"))
    if not baseline.is_file() or not files:
        raise MigrationError(f"Не найдены схема и миграции в {db_dir}")
    for number, file in enumerate(files, start=2):
        if not re.fullmatch(rf"{number:03d}_[a-z0-9_]+\.sql", file.name):
            raise MigrationError(f"Нарушен порядок миграций: {file.name}")
    return [Migration(BASELINE, baseline.read_text(encoding="utf-8")), *(
        Migration(file.name, file.read_text(encoding="utf-8")) for file in files)]


def check_current(cursor, db_dir: Path = DB_DIR) -> None:
    """Проверка при старте API: ничего в базе не меняет.

    Более новые миграции допускаются: старый API можно вернуть после
    совместимых добавлений в схему. Свои миграции должны присутствовать целиком.
    """
    expected = load(db_dir)
    cursor.execute("SELECT to_regclass('schema_migration') AS registry")
    if cursor.fetchone()["registry"] is None:
        raise MigrationError("Нет истории схемы. Выполните tools/migrate.sh перед запуском API")
    try:
        cursor.execute("SELECT name, checksum FROM schema_migration")
    except psycopg.errors.UndefinedColumn as error:
        raise MigrationError("Обновите историю схемы командой tools/migrate.sh") from error
    history = {row["name"]: row["checksum"] for row in cursor.fetchall()}
    for migration in expected:
        # На действующем сервере базовая схема применялась до появления журнала.
        if migration.name == BASELINE and BASELINE not in history:
            continue
        if migration.name not in history:
            raise MigrationError(f"Не применена {migration.name}. Выполните tools/migrate.sh")
        if history[migration.name] != migration.checksum:
            raise MigrationError(f"Не совпадает SHA-256 {migration.name}. Выполните tools/migrate.sh")


def apply(connection, db_dir: Path = DB_DIR, report: Callable[[str], None] = print) -> list[str]:
    """Применяет схему с нуля или продолжает существующую историю.

    Соединение с autocommit: отдельная транзакция на файл, одна сессионная
    блокировка на весь запуск. Запись в журнале фиксируется вместе с SQL.
    """
    if not connection.autocommit:
        raise ValueError("Миграциям требуется соединение с autocommit=True")
    expected = load(db_dir)
    applied: list[str] = []
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SET lock_timeout = '60s'")
        cursor.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            with connection.transaction():
                cursor.execute("CREATE TABLE IF NOT EXISTS schema_migration ("
                               "name TEXT PRIMARY KEY,"
                               "applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), checksum TEXT)")
                cursor.execute("ALTER TABLE schema_migration ADD COLUMN IF NOT EXISTS checksum TEXT")
            cursor.execute("SELECT name, checksum FROM schema_migration")
            history = {row["name"]: row["checksum"] for row in cursor.fetchall()}
            unknown = set(history) - {item.name for item in expected}
            if unknown:
                raise MigrationError("База новее набора миграций: " + ", ".join(sorted(unknown)))
            pending_seen = False
            for item in expected[1:]:
                if item.name not in history:
                    pending_seen = True
                elif pending_seen:
                    raise MigrationError(f"Пропуск в истории перед {item.name}")
            for item in expected:
                checksum = history.get(item.name)
                if checksum is not None and checksum != item.checksum:
                    raise MigrationError(f"Изменена применённая миграция {item.name}")
            cursor.execute("SELECT to_regclass('app_user') AS users")
            fresh = cursor.fetchone()["users"] is None
            if fresh and history:
                raise MigrationError("История миграций есть, но базовая таблица app_user отсутствует")
            if not fresh and not history:
                raise MigrationError("База не пуста, но история миграций отсутствует; восстановите журнал")
            for item in expected:
                if item.name in history:
                    if history[item.name] is None:
                        # Старый shell-скрипт хешей не хранил. SQL повторно не
                        # исполняем: в нём есть изменения данных, не только DDL.
                        with connection.transaction():
                            cursor.execute("UPDATE schema_migration SET checksum = %s WHERE name = %s",
                                           (item.checksum, item.name))
                        report(f"SHA-256 записан: {item.name}")
                    continue
                if item.name == BASELINE and not fresh:
                    continue
                report(f"Применяется {item.name}")
                with connection.transaction():
                    cursor.execute(item.sql, prepare=False)
                    cursor.execute("INSERT INTO schema_migration (name, checksum) VALUES (%s, %s)",
                                   (item.name, item.checksum))
                applied.append(item.name)
        finally:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    report(f"Применено миграций: {len(applied)}")
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="только проверить готовность схемы")
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        parser.error("DATABASE_URL не задан")
    try:
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=10) as connection:
            if args.check:
                with connection.cursor() as cursor:
                    check_current(cursor)
                print("Схема готова")
            else:
                apply(connection)
    except (MigrationError, psycopg.Error) as error:
        print(f"Миграции: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
