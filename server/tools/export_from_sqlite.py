"""Выгрузка локальной базы оплат в файл для загрузки в PostgreSQL.

Скрипт ничего не меняет: читает `%APPDATA%\\RetailCore\\payments.db` и пишет
рядом .sql, который применяется на сервере через psql. Разделение на «выгрузить»
и «применить» намеренное — получившийся файл можно прочитать глазами до того,
как он попадёт в общую базу.

Формат — COPY ... FROM stdin, а не набор INSERT: 6958 строк вставляются одной
командой за десятые доли секунды, и psql не приходится разбирать семь тысяч
операторов. Экранирование текста делается по правилам текстового формата COPY,
иначе комментарий с переводом строки развалил бы файл.

Идентификаторы платежей сохраняются как есть: на них ссылаются вложения, и
перенумерация порвала бы связь. Последовательности после загрузки сдвигаются
на максимум, чтобы следующая запись не наткнулась на занятый номер.

Запуск:
    python server/tools/export_from_sqlite.py [--source path] [--out path]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal

# Порядок столбцов в каждой выгрузке. Он же попадает в заголовок COPY, поэтому
# менять его можно только вместе со схемой.
PAYMENT_COLUMNS = (
    "id", "doc_number", "request_date", "pay_date", "amount", "vat", "currency",
    "supplier_id", "recipient", "recipient_key", "status", "source_status",
    "paid_flag", "operation", "over_limit", "priority", "edo_state",
    "responsible", "author", "comment", "had_files", "origin", "origin_ref",
    "created_at", "updated_at",
)

DATE_COLUMNS = frozenset({"request_date", "pay_date"})
MONEY_COLUMNS = frozenset({"amount", "vat"})
FLAG_COLUMNS = frozenset({"paid_flag", "over_limit", "had_files"})
MOMENT_COLUMNS = frozenset({"created_at", "updated_at"})


def default_source() -> str:
    profile = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(profile, "RetailCore", "payments.db")


def escape(value: object, column: str) -> str:
    """Одно значение в текстовом формате COPY. Пустая дата становится NULL."""
    if value is None:
        return r"\N"
    if column in DATE_COLUMNS and not str(value).strip():
        return r"\N"
    if column in MONEY_COLUMNS:
        # Суммы лежали во float. Округление до копейки здесь — не потеря
        # точности, а её восстановление: копейки и есть та точность, с которой
        # оплата пришла из 1С.
        return f"{float(value):.2f}"
    if column in FLAG_COLUMNS:
        return "t" if int(value) else "f"
    if column in MOMENT_COLUMNS and not str(value).strip():
        raise ValueError(f"пустое значение {column} — схема его не допускает")

    text = str(value)
    # Порядок важен: обратный слэш экранируется первым, иначе он испортит
    # экранирование, добавленное следом.
    text = text.replace("\\", "\\\\")
    text = text.replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    return text


def copy_block(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    query: str,
) -> tuple[str, int]:
    """Готовит один блок COPY и считает выгруженные строки."""
    lines = [f"COPY {table} ({', '.join(columns)}) FROM stdin;"]
    rows = 0
    for row in connection.execute(query):
        lines.append("\t".join(escape(row[name], name) for name in columns))
        rows += 1
    lines.append("\\.")
    return "\n".join(lines), rows


def files_block(connection: sqlite3.Connection, counts: dict[str, int]) -> str:
    """Вложения. Путь с диска автора заменяется именем файла в хранилище.

    «C:\\Users\\e.ivanov\\...» на чужой машине не открывается, поэтому на сервер
    едет только имя; сами файлы копируются отдельно, копией папки payment_files.
    """
    columns = ("id", "payment_id", "name", "stored_as", "size", "added_at")
    lines = [f"COPY payment_file ({', '.join(columns)}) FROM stdin;"]
    rows = 0
    query = ("SELECT id, payment_id, name, path, size, added_at"
             " FROM payment_file ORDER BY id")
    for row in connection.execute(query):
        stored = os.path.basename(str(row["path"]).replace("\\", "/"))
        values = {**{name: row[name] for name in columns if name != "stored_as"},
                  "stored_as": stored}
        lines.append("\t".join(escape(values[name], name) for name in columns))
        rows += 1
    lines.append("\\.")
    counts["payment_file"] = rows
    return "\n".join(lines)


def build(source: str) -> tuple[str, dict[str, int], Decimal]:
    connection = sqlite3.connect(source)
    connection.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    blocks: list[str] = []

    payments, counts["payment"] = copy_block(
        connection, "payment", PAYMENT_COLUMNS,
        f"SELECT {', '.join(PAYMENT_COLUMNS)} FROM payment ORDER BY id")
    blocks.append(payments)

    blocks.append(files_block(connection, counts))

    budgets, counts["budget"] = copy_block(
        connection, "budget", ("year", "month", "amount", "note", "updated_at"),
        "SELECT year, month, amount, note, updated_at FROM budget"
        " ORDER BY year, month")
    blocks.append(budgets)

    links, counts["recipient_link"] = copy_block(
        connection, "recipient_link",
        ("recipient_key", "recipient", "supplier_id", "linked_by", "updated_at"),
        "SELECT recipient_key, recipient, supplier_id, linked_by, updated_at"
        " FROM recipient_link ORDER BY recipient_key")
    blocks.append(links)

    runs, counts["import_run"] = copy_block(
        connection, "import_run",
        ("id", "path", "file_hash", "rows_total", "rows_new", "rows_updated",
         "rows_same", "rows_skipped", "error", "finished_at"),
        "SELECT id, path, file_hash, rows_total, rows_new, rows_updated,"
        " rows_same, rows_skipped, error, finished_at FROM import_run ORDER BY id")
    blocks.append(runs)

    # Контрольная сумма считается по тем же округлённым значениям, что уезжают
    # в файл, — иначе сверка ловила бы разницу округления, а не потерю данных.
    total = Decimal("0")
    for row in connection.execute("SELECT amount FROM payment"):
        total += Decimal(f"{float(row['amount']):.2f}")
    connection.close()

    header = (
        "-- Выгрузка локальной базы оплат RetailCore.\n"
        f"-- Источник: {source}\n"
        f"-- Создано:  {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"-- Платежей: {counts['payment']}, сумма: {total}\n"
        "--\n"
        "-- Применяется в одной транзакции: либо переносится всё, либо ничего.\n"
        "BEGIN;\n"
    )
    # Последовательности сдвигаются после загрузки: до неё максимум неизвестен,
    # а BIGSERIAL продолжил бы нумерацию с единицы и упёрся в занятые номера.
    footer = (
        "\nSELECT setval(pg_get_serial_sequence('payment', 'id'),"
        " GREATEST((SELECT MAX(id) FROM payment), 1));\n"
        "SELECT setval(pg_get_serial_sequence('payment_file', 'id'),"
        " GREATEST((SELECT MAX(id) FROM payment_file), 1));\n"
        "SELECT setval(pg_get_serial_sequence('import_run', 'id'),"
        " GREATEST((SELECT MAX(id) FROM import_run), 1));\n"
        "COMMIT;\n"
    )
    return header + "\n".join(blocks) + footer, counts, total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=default_source(),
                        help="путь к payments.db (по умолчанию — папка профиля)")
    parser.add_argument("--out", default="payments_dump.sql",
                        help="куда записать выгрузку")
    options = parser.parse_args()

    if not os.path.isfile(options.source):
        print(f"База не найдена: {options.source}", file=sys.stderr)
        return 1

    text, counts, total = build(options.source)
    with open(options.out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)

    print(f"Источник: {options.source}")
    for table, rows in counts.items():
        print(f"  {table}: {rows}")
    print(f"Контрольная сумма оплат: {total}")
    print(f"Записано: {options.out} ({os.path.getsize(options.out)} байт)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
