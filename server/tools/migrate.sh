#!/bin/sh
# Применение миграций схемы.
#
# schema.sql применяется только к пустому тому (docker-entrypoint-initdb.d), а
# база уже не пуста. Дальнейшие изменения — файлами в db/migrations, по порядку
# имён, каждый в своей транзакции.
#
# Применённые миграции запоминаются: повторный запуск ничего не переделывает,
# а частично применённый набор можно докатить, не разбираясь вручную.
set -eu

ROOT=/opt/retailcore
cd "$ROOT"

psql() {
    docker compose exec -T db psql -U retailcore -d retailcore -v ON_ERROR_STOP=1 "$@"
}

psql -q -c "CREATE TABLE IF NOT EXISTS schema_migration (
    name       TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);"

applied=0
for file in db/migrations/*.sql; do
    [ -e "$file" ] || continue
    name=$(basename "$file")
    known=$(psql -t -A -c "SELECT 1 FROM schema_migration WHERE name = '$name';")
    if [ -n "$known" ]; then
        echo "· $name — уже применена"
        continue
    fi
    echo "→ $name"
    # Миграция и отметка о ней — в одной транзакции: иначе прерванный запуск
    # оставил бы применённые изменения без записи, и второй прогон повторил бы их.
    { echo "BEGIN;"; cat "$file"
      echo "INSERT INTO schema_migration (name) VALUES ('$name');"
      echo "COMMIT;"; } | psql -q
    applied=$((applied + 1))
done

echo "применено миграций: $applied"
