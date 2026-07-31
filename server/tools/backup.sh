#!/bin/sh
# Ночная резервная копия базы оплат и вложений.
#
# База переехала с рабочего компьютера на сервер, и вместе с ней переехал риск:
# раньше payments.db лежал в профиле пользователя и попадал в копию вместе с
# остальным профилем, теперь у него нет никакой защиты, кроме этой.
#
# Копия делается pg_dump внутри контейнера, а не копированием файлов тома:
# скопированный «на живую» каталог PostgreSQL восстановлению не подлежит.
#
# Ставится в cron строкой:
#   0 3 * * * /opt/retailcore/tools/backup.sh >> /var/log/retailcore-backup.log 2>&1
set -eu

ROOT=/opt/retailcore
STORE=$ROOT/backups
KEEP_DAYS=30
STAMP=$(date +%Y-%m-%d)

mkdir -p "$STORE"
cd "$ROOT"

# --clean --if-exists: дамп сам приводит базу в исходное состояние, и
# восстанавливать его можно поверх непустой базы, не удаляя её руками.
docker compose exec -T db pg_dump -U retailcore -d retailcore \
    --clean --if-exists --no-owner \
    | gzip -9 > "$STORE/retailcore-$STAMP.sql.gz.part"

# Переименование в готовое имя — последним шагом: прерванная посреди работы
# копия не должна выглядеть как пригодная к восстановлению.
mv "$STORE/retailcore-$STAMP.sql.gz.part" "$STORE/retailcore-$STAMP.sql.gz"

# Вложения — отдельным архивом: они меняются реже базы и жмутся хуже.
tar -czf "$STORE/files-$STAMP.tar.gz" -C "$ROOT" files

find "$STORE" -name '*.gz' -mtime +$KEEP_DAYS -delete
find "$STORE" -name '*.part' -mtime +1 -delete

echo "$(date '+%Y-%m-%d %H:%M') копия готова: $(du -sh "$STORE" | cut -f1) всего"
