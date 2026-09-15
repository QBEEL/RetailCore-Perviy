#!/bin/sh
# Проверка вкладки поставщиков на живой базе.
#
# Запускается после `tools/migrate.sh` и отвечает на один вопрос: работает ли
# то, что написано, на настоящих данных. Проверяются три вещи — схема встала,
# большой запрос списка выполняется, закрепление проходит полный круг.
#
# Запрос берётся из самого модуля, а не переписывается сюда: копия рано или
# поздно разойдётся с кодом, и проверка начнёт подтверждать не то, что работает
# в приложении.
#
# Данные не меняются. Круг закрепления идёт в транзакции с откатом: заявка,
# фиксация и пересчёт направлений выполняются по-настоящему, но в базе после
# проверки не остаётся ничего.
set -eu

ROOT=/opt/retailcore
cd "$ROOT"

echo "--- схема ---"
docker compose exec -T db psql -U retailcore -d retailcore -v ON_ERROR_STOP=1 -c "
SELECT table_name FROM information_schema.tables
 WHERE table_name IN ('direction', 'user_direction', 'supplier_assignment',
                      'supplier_direction', 'supplier_assignment_log')
 ORDER BY table_name;"

docker compose exec -T db psql -U retailcore -d retailcore -v ON_ERROR_STOP=1 -c "
SELECT code, title FROM direction ORDER BY sort_order;"

echo "--- список поставщиков ---"
docker compose exec -T api python - <<'PY'
from app import db
from app.routers.suppliers import _LIST_CTE, _SORTS, _filters

db.pool.open(wait=True, timeout=30)

# Отбор без условий: он же выполняется при открытии вкладки у администратора.
where, values = _filters("", "", 0, "")
total = db.fetch_one(f"{_LIST_CTE} SELECT COUNT(*) AS n FROM j{where}", values)
print(f"поставщиков всего: {total['n']}")

rows = db.fetch_all(
    f"{_LIST_CTE} SELECT * FROM j{where}"
    f" ORDER BY {_SORTS['amount']} DESC NULLS LAST, j.recipient LIMIT 5", values)
for row in rows:
    print(f"  {row['recipient'][:38]:<38} {float(row['amount'] or 0):>14,.0f}"
          f"  платили: {len(row['managers'] or [])}"
          f"  ведут: {len(row['assigned'] or [])}"
          f"  {','.join(row['directions'] or []) or '—'}")

# Каждый отбор отдельно: ошибка в условии видна сразу, а не общим падением.
for name, args in (("по направлению", ("", "beauty", 0, "")),
                   ("по менеджеру", ("", "", 1, "")),
                   ("зафиксированные", ("", "", 0, "fixed")),
                   ("черновики", ("", "", 0, "draft")),
                   ("без закрепления", ("", "", 0, "none")),
                   ("поиск", ("ООО", "", 0, ""))):
    where, values = _filters(*args)
    row = db.fetch_one(f"{_LIST_CTE} SELECT COUNT(*) AS n FROM j{where}", values)
    print(f"  отбор {name}: {row['n']}")

# Плательщики, сведённые к учётным записям. Ради этого и затевалась
# нормализация: в выгрузке 1С один человек записан десятком написаний.
rows = db.fetch_all(
    "SELECT COUNT(DISTINCT p.responsible) AS spellings,"
    "       COUNT(DISTINCT ur.user_id) AS people"
    " FROM payment p LEFT JOIN user_responsible ur ON ur.responsible = p.responsible"
    " WHERE p.responsible <> ''")
print(f"написаний в 1С: {rows[0]['spellings']}, из них сведено к учёткам:"
      f" {rows[0]['people']}")

unknown = db.fetch_all(
    "SELECT DISTINCT p.responsible FROM payment p"
    " WHERE p.responsible <> '' AND NOT EXISTS ("
    "   SELECT 1 FROM user_responsible ur WHERE ur.responsible = p.responsible)"
    " ORDER BY 1")
if unknown:
    print(f"не сведены к учётным записям ({len(unknown)}):")
    for row in unknown[:15]:
        print(f"  · {row['responsible']}")
PY

echo "--- круг закрепления (в транзакции с откатом) ---"
docker compose exec -T api python - <<'PY'
from app import db
from app.routers.suppliers import _sync_directions

db.pool.open(wait=True, timeout=30)

with db.pool.connection() as connection:
    with connection.cursor() as handle:
        handle.execute("SELECT recipient_key, recipient FROM payment"
                       " WHERE recipient <> '' LIMIT 1")
        target = handle.fetchone()
        handle.execute("SELECT id, full_name FROM app_user"
                       " WHERE is_active ORDER BY id LIMIT 1")
        person = handle.fetchone()
        if not target or not person:
            print("нет данных для проверки: пустые оплаты или учётные записи")
            raise SystemExit(0)

        key, user_id = target["recipient_key"], person["id"]
        print(f"поставщик: {target['recipient']}  менеджер: {person['full_name']}")

        handle.execute(
            "INSERT INTO supplier_assignment (recipient_key, user_id, claimed_by)"
            " SELECT k, %s::bigint, %s::bigint FROM unnest(%s::text[]) AS k"
            " ON CONFLICT (recipient_key, user_id) DO NOTHING"
            " RETURNING state", (user_id, user_id, [key]))
        print(f"  заявка: {handle.fetchone()['state']}")

        _sync_directions(handle, [key])
        handle.execute("SELECT d.code, sd.source FROM supplier_direction sd"
                       " JOIN direction d ON d.id = sd.direction_id"
                       " WHERE sd.recipient_key = %s", (key,))
        got = handle.fetchall()
        print(f"  направления после заявки: "
              f"{', '.join(r['code'] + '/' + r['source'] for r in got) or '—'}"
              f"  (пусто — у менеджера не задано направление)")

        handle.execute(
            "UPDATE supplier_assignment SET state = 'fixed', fixed_at = now(),"
            " fixed_by = %s WHERE recipient_key = %s AND state = 'draft'"
            " RETURNING state", (user_id, key))
        print(f"  фиксация: {handle.fetchone()['state']}")

        handle.execute(
            "INSERT INTO supplier_assignment_log"
            " (recipient_key, user_id, action, payload, actor_id)"
            " SELECT k, %s::bigint, %s::text, %s::jsonb, %s::bigint"
            " FROM unnest(%s::text[]) AS k",
            (user_id, "fix", "{}", user_id, [key]))
        print("  журнал: записан")

        # Проверка закончена — база остаётся такой же, какой была.
        connection.rollback()
        print("  откат выполнен")

row = db.fetch_one("SELECT COUNT(*) AS n FROM supplier_assignment")
print(f"закреплений в базе после проверки: {row['n']}")
PY

echo "--- готово ---"
