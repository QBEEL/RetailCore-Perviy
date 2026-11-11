#!/bin/sh
# Ручной запуск того же мигратора, который блокирует старт API в compose.
set -eu

ROOT=${RETAILCORE_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}
cd "$ROOT"
docker compose run --rm --build migrate python -m app.migrations "$@"
