"""Черновик учётных записей по данным оплат.

В выгрузке 1С ответственный записан строкой с ФИО — это и есть единственный
след того, кто ведёт поставщика. Скрипт превращает эти строки в заготовки
учёток: логин, отображаемое имя и признак, нужен ли человеку доступ.

Черновик, а не готовый список: кто уволился и кто просто давно не платил,
данные не различают. Решение принимает человек, скрипт лишь показывает, на
что смотреть — сколько оплат и когда была последняя.

Логин строится как «первая буква имени. фамилия» латиницей — так же, как
заведены рабочие учётные записи (Иванов Евгений → e.ivanov).

Запуск:
    python server/tools/draft_accounts.py [--source path] [--months 12]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date

# Служебные значения: это не люди, и учётки им не нужны.
NOT_PEOPLE = frozenset({"Программист", "ITP", ""})

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# «Л?вкина» — в выгрузке 1С буква «ё» потерялась и осталась знаком вопроса.
# Чинится здесь, потому что иначе логин получится с дырой посередине.
BROKEN_LETTER = "?"


def transliterate(text: str) -> str:
    return "".join(TRANSLIT.get(ch, ch if ch.isalnum() else "") for ch in text.lower())


def split_name(value: str) -> tuple[str, str]:
    """Строка ответственного → (фамилия, имя). Скобки и отчество отбрасываются.

    В данных встречается «Семыкина (Брыкова) Алина» — девичья фамилия в скобках,
    и «Беккелеева Анна (Управляющий Артем, Аэро Вл)» — пометка о точке продаж.
    И то и другое к имени отношения не имеет.
    """
    clean = re.sub(r"\([^)]*\)", " ", value)
    clean = clean.replace(BROKEN_LETTER, "ё")
    parts = [p for p in clean.split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def make_login(surname: str, first: str, taken: set[str]) -> str:
    """«Иванов Евгений» → e.ivanov. Совпадения разводятся цифрой."""
    sur = transliterate(surname)
    initial = transliterate(first)[:1]
    if not sur:
        return ""
    base = f"{initial}.{sur}" if initial else sur
    login, extra = base, 2
    while login in taken:
        login, extra = f"{base}{extra}", extra + 1
    taken.add(login)
    return login


def months_before(moment: date, months: int) -> date:
    """Дата на `months` месяцев назад. 31 марта минус месяц — 28 (или 29) февраля."""
    total = moment.year * 12 + (moment.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    last_day = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(moment.day, last_day))


def collect(source: str, months: int, today: date) -> list[dict]:
    connection = sqlite3.connect(source)
    connection.row_factory = sqlite3.Row
    edge_text = months_before(today, months).isoformat()

    rows = connection.execute(
        "SELECT responsible,"
        "       COUNT(*) AS total,"
        "       SUM(CASE WHEN pay_date >= ? THEN 1 ELSE 0 END) AS recent,"
        "       MAX(pay_date) AS last_date,"
        "       ROUND(SUM(amount), 2) AS amount"
        " FROM payment WHERE responsible <> ''"
        " GROUP BY responsible ORDER BY recent DESC, total DESC",
        (edge_text,),
    ).fetchall()
    connection.close()

    taken: set[str] = set()
    draft: list[dict] = []
    for row in rows:
        value = row["responsible"]
        if value in NOT_PEOPLE:
            continue
        surname, first = split_name(value)
        draft.append({
            "login": make_login(surname, first, taken),
            "full_name": f"{surname} {first}".strip(),
            # Список, а не строка: если в 1С появится второе написание того же
            # человека, его добавляют сюда, и права считаются по обоим.
            "responsible": [value],
            "is_admin": False,
            # Учётка заводится тем, кто платил за последний год. Остальные
            # остаются в истории, но войти не смогут.
            "is_active": bool(row["recent"]),
            "_payments": row["total"],
            "_recent": row["recent"],
            "_last_date": row["last_date"] or "",
            "_amount": float(row["amount"] or 0),
        })
    return draft


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    profile = os.environ.get("APPDATA") or os.path.expanduser("~")
    parser.add_argument("--source",
                        default=os.path.join(profile, "RetailCore", "payments.db"))
    parser.add_argument("--months", type=int, default=12,
                        help="за сколько месяцев считать человека действующим")
    parser.add_argument("--admin", default="Иванов Евгений",
                        help="кому поставить признак администратора")
    # По умолчанию — в папку профиля, а не в репозиторий: в черновике ФИО
    # полусотни сотрудников, а репозиторий публичный.
    parser.add_argument("--out",
                        default=os.path.join(profile, "RetailCore",
                                             "accounts.draft.json"))
    options = parser.parse_args()

    if not os.path.isfile(options.source):
        print(f"База не найдена: {options.source}", file=sys.stderr)
        return 1

    draft = collect(options.source, options.months, date.today())
    for account in draft:
        if options.admin in account["responsible"]:
            account["is_admin"] = True
            account["is_active"] = True

    os.makedirs(os.path.dirname(options.out) or ".", exist_ok=True)
    with open(options.out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(draft, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    active = [a for a in draft if a["is_active"]]
    print(f"{'логин':<22} {'ФИО':<34} {'оплат':>6} {'за год':>7} {'последняя':>11}")
    print("-" * 84)
    for account in draft:
        mark = " " if account["is_active"] else "·"
        print(f"{mark}{account['login']:<21} {account['full_name']:<34}"
              f" {account['_payments']:>6} {account['_recent']:>7}"
              f" {account['_last_date']:>11}")
    print("-" * 84)
    print(f"всего людей: {len(draft)}, с доступом: {len(active)},"
          f" помечены точкой (без доступа): {len(draft) - len(active)}")
    print(f"Записано: {options.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
