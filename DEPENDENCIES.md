# Зависимости Python

Рабочая версия Python для CI и сервера — CPython 3.12. Клиент также проверяется
локально на CPython 3.14. Windows и macOS используют один набор версий клиента;
пакеты, нужные только одной системе, отмечены условиями в requirements.

## Установка

Файлы `.txt` содержат точные версии всех зависимостей и SHA-256 пакетов.
Установка выполняется через pip; uv нужен только при обновлении этих файлов.

| Назначение | Файл |
|---|---|
| Запуск клиента | `requirements.txt` |
| Сборка клиента, включая PyInstaller | `requirements-build.txt` |
| Тесты клиента и сервера | `requirements-test.txt` |
| Серверный контейнер | `server/api/requirements.txt` |

Из корня проекта, в выбранном виртуальном окружении:

```sh
python -m pip install --require-hashes --only-binary=:all: -r requirements-test.txt
python -m pip check
python -m pytest tests -q -ra
```

Для сборки установите `requirements-build.txt` той же командой. CI и Docker
используют эти файлы с проверкой хешей. `--only-binary=:all:` запрещает незаметно
собирать зависимость из исходников с незакреплёнными инструментами сборки.

## Обновление

Исходные ограничения версий записаны в `.in`; `.txt` генерируются, вручную их
править не нужно. Наборы сборки и тестов включают закреплённые зависимости
клиента; тесты также включают сервер. При генерации серверного набора общие
библиотеки ограничены версиями клиента. Готовый серверный `.txt` самодостаточен
и не требует клиентских файлов внутри Docker.

Для генерации использован uv 0.11.25. Команды из корня проекта:

```sh
uv pip compile requirements.in --universal --python-version 3.12 --generate-hashes --no-annotate -o requirements.txt
uv pip compile server/api/requirements.in --universal --python-version 3.12 --generate-hashes --no-annotate -o server/api/requirements.txt
uv pip compile requirements-build.in --universal --python-version 3.12 --generate-hashes --no-annotate -o requirements-build.txt
uv pip compile requirements-test.in --universal --python-version 3.12 --generate-hashes --no-annotate -o requirements-test.txt
```

Генератор сохраняет версии из существующего `.txt`, пока они удовлетворяют
ограничениям. Для обновления конкретного пакета добавьте `--upgrade-package ИМЯ`
к соответствующей команде; для обновления всего набора — `--upgrade`.
Сначала обновляйте клиент, затем сервер, затем сборку и тесты, чтобы все наборы
согласовались. После изменения `.in` повторите генерацию в этом порядке.

Проверяйте обновление в новом виртуальном окружении: установите тестовый и
сборочный наборы, выполните `pip check` и полный прогон тестов. Перед выпуском
дождитесь тестов и сборок Windows/macOS в GitHub Actions. Проверка реальной
сборки контейнера дополнительно нужна для изменений серверных зависимостей.

Фиксация Python-пакетов не фиксирует ОС, версию Python до патча или базовый
Docker-образ; побайтовая идентичность exe и контейнера этим не гарантируется.
