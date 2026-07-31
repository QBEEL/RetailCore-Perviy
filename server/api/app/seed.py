"""Заведение учётных записей из подготовленного списка.

Список готовится на рабочем компьютере (`server/tools/draft_accounts.py`) и
вычитывается человеком: данные не отличают уволившегося от того, кто просто
давно не платил. Здесь только запись в базу.

Пароли печатаются один раз — в базе остаётся лишь хеш. Повторный запуск
существующие учётки не трогает и пароли не сбрасывает: иначе случайный второй
запуск разом отключил бы всем вход.

Список подаётся на вход, а не кладётся файлом: в нём ФИО сотрудников, и
оставлять его на диске сервера незачем — после заведения учёток он не нужен.

Запуск на сервере:
    docker compose exec -T api python -m app.seed - < accounts.json
"""
from __future__ import annotations

import json
import sys

from . import db, security


def seed(path: str) -> int:
    if path == "-":
        accounts = json.load(sys.stdin)
    else:
        with open(path, encoding="utf-8") as handle:
            accounts = json.load(handle)

    db.pool.open(wait=True, timeout=30)
    created: list[tuple[str, str, str]] = []
    skipped: list[str] = []

    for account in accounts:
        login = account["login"].strip().lower()
        if not login:
            continue
        if not account.get("is_active", True):
            continue
        if db.fetch_one("SELECT 1 FROM app_user WHERE login = %s", (login,)):
            skipped.append(login)
            continue

        password = security.random_password()
        row = db.fetch_one(
            "INSERT INTO app_user (login, full_name, password_hash, is_admin,"
            "                      is_active)"
            " VALUES (%s, %s, %s, %s, TRUE) RETURNING id",
            (login, account.get("full_name", ""),
             security.hash_password(password), account.get("is_admin", False)))
        for name in account.get("responsible", []):
            db.execute("INSERT INTO user_responsible (user_id, responsible)"
                       " VALUES (%s, %s) ON CONFLICT DO NOTHING",
                       (row["id"], name))
        created.append((login, account.get("full_name", ""), password))

    print(f"{'логин':<20} {'ФИО':<26} пароль")
    print("-" * 62)
    for login, name, password in created:
        print(f"{login:<20} {name:<26} {password}")
    print("-" * 62)
    print(f"заведено: {len(created)}, уже существовали: {len(skipped)}")
    if skipped:
        print("пропущены:", ", ".join(skipped))
    db.pool.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Использование: python -m app.seed <файл.json>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(seed(sys.argv[1]))
