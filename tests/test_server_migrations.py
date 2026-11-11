"""Миграции на настоящем PostgreSQL; RETAILCORE_TEST_DSN указывает тестовый сервер.

Каждый тест создаёт отдельную базу со случайным именем и удаляет только её.
В CI эти проверки обязательны в отдельной задаче с PostgreSQL 16.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "server" / "api" / "app"
DB = ROOT / "server" / "db"
_spec = importlib.util.spec_from_file_location("retail_migrations", APP / "migrations.py")
migrations = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = migrations
_spec.loader.exec_module(migrations)

pytestmark = pytest.mark.skipif(
    not os.environ.get("RETAILCORE_TEST_DSN"), reason="нужен тестовый PostgreSQL: RETAILCORE_TEST_DSN")


@pytest.fixture
def database():
    name = "retailcore_test_" + uuid.uuid4().hex
    with psycopg.connect(os.environ["RETAILCORE_TEST_DSN"], autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'")
                      .format(sql.Identifier(name)))
        try:
            dsn = make_conninfo(os.environ["RETAILCORE_TEST_DSN"], dbname=name)
            with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as connection:
                yield dsn, connection
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def db_files(tmp_path):
    return Path(shutil.copytree(DB, tmp_path / "db"))


def migrate(connection, directory=DB):
    return migrations.apply(connection, directory, report=lambda message: None)


def history(connection):
    return connection.execute("SELECT * FROM schema_migration ORDER BY name").fetchall()


def test_empty_database_reaches_current_schema_and_seeds(database):
    _, connection = database
    applied = migrate(connection)
    assert applied == [item.name for item in migrations.load(DB)]
    assert len(applied) == 20
    with connection.cursor() as cursor:
        migrations.check_current(cursor)
    assert connection.execute("SELECT count(*) AS n FROM claim_brand").fetchone()["n"] > 600
    assert connection.execute("SELECT count(*) AS n FROM dds_item").fetchone()["n"] > 100
    user_id = connection.execute(
        "INSERT INTO app_user(login,password_hash) VALUES ('warehouse','hash') RETURNING id"
    ).fetchone()["id"]
    # Последняя миграция должна разрешить излишек и сохранить код маркировки.
    connection.execute("INSERT INTO claim(kind,applicant_id,mark_code) VALUES ('surplus',%s,'mark')",
                       (user_id,))


def test_repeat_preserves_data_and_migration_timestamps(database):
    _, connection = database
    migrate(connection)
    connection.execute("INSERT INTO app_user(login,password_hash,must_change_password) "
                       "VALUES ('manager','hash',FALSE)")
    before = history(connection)
    assert migrate(connection) == []
    assert history(connection) == before
    assert not connection.execute("SELECT must_change_password FROM app_user").fetchone()["must_change_password"]


def test_missing_history_is_not_guessed_from_existing_tables(database):
    _, connection = database
    migrate(connection)
    connection.execute("INSERT INTO app_user(login,password_hash,must_change_password) "
                       "VALUES ('manager','hash',FALSE)")
    connection.execute("DROP TABLE schema_migration")
    with pytest.raises(migrations.MigrationError, match="восстановите журнал"):
        migrate(connection)
    assert not connection.execute("SELECT must_change_password FROM app_user").fetchone()["must_change_password"]


def test_upgrade_preserves_user_and_money(database, db_files):
    _, connection = database
    for file in (db_files / "migrations").glob("*.sql"):
        if int(file.name[:3]) > 8:
            file.unlink()
    migrate(connection, db_files)
    connection.execute("INSERT INTO app_user(login,password_hash) VALUES ('existing','hash')")
    connection.execute("INSERT INTO payment(amount,recipient,comment) VALUES (12345.67,'supplier','keep')")
    with connection.cursor() as cursor, pytest.raises(migrations.MigrationError, match="009"):
        migrations.check_current(cursor)
    migrate(connection)
    assert connection.execute("SELECT login FROM app_user").fetchone()["login"] == "existing"
    payment = connection.execute("SELECT amount,comment FROM payment").fetchone()
    assert str(payment["amount"]) == "12345.67" and payment["comment"] == "keep"


def test_old_shell_history_is_adopted_without_replaying_sql(database):
    _, connection = database
    connection.execute("CREATE TABLE schema_migration(name TEXT PRIMARY KEY,"
                       "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    for item in migrations.load(DB):
        connection.execute(item.sql, prepare=False)
        if item.name != migrations.BASELINE:
            connection.execute("INSERT INTO schema_migration(name) VALUES (%s)", (item.name,))
    connection.execute("INSERT INTO app_user(login,password_hash,must_change_password) "
                       "VALUES ('already_changed','hash',FALSE)")
    with connection.cursor() as cursor, pytest.raises(migrations.MigrationError, match="историю"):
        migrations.check_current(cursor)
    before = {row["name"]: row["applied_at"] for row in history(connection)}
    assert migrate(connection) == []
    assert {row["name"]: row["applied_at"] for row in history(connection)} == before
    assert all(row["checksum"] for row in history(connection))
    assert not connection.execute("SELECT must_change_password FROM app_user").fetchone()["must_change_password"]
    with connection.cursor() as cursor:
        migrations.check_current(cursor)


def test_failed_file_rolls_back_sql_and_history_then_can_resume(database, db_files):
    _, connection = database
    failed = db_files / "migrations" / "021_failure.sql"
    failed.write_text("CREATE TABLE migration_probe(id INT); SELECT 1/0;", encoding="utf-8")
    with pytest.raises(psycopg.errors.DivisionByZero):
        migrate(connection, db_files)
    assert len(history(connection)) == 20
    assert connection.execute("SELECT to_regclass('migration_probe') AS t").fetchone()["t"] is None
    failed.write_text("CREATE TABLE migration_probe(id INT);", encoding="utf-8")
    assert migrate(connection, db_files) == [failed.name]
    assert len(history(connection)) == 21


def test_changed_applied_file_is_rejected_before_pending_sql(database, db_files):
    _, connection = database
    migrate(connection, db_files)
    changed = db_files / "migrations" / "020_claim_marking.sql"
    changed.write_text(changed.read_text(encoding="utf-8") + "\n-- changed\n", encoding="utf-8")
    (db_files / "migrations" / "021_pending.sql").write_text("CREATE TABLE should_not_exist(id INT);", encoding="utf-8")
    with pytest.raises(migrations.MigrationError, match="020"):
        migrate(connection, db_files)
    assert connection.execute("SELECT to_regclass('should_not_exist') AS t").fetchone()["t"] is None
    with connection.cursor() as cursor, pytest.raises(migrations.MigrationError, match="SHA-256"):
        migrations.check_current(cursor, db_files)


def test_newline_conversion_does_not_change_checksum(database, db_files):
    _, connection = database
    migrate(connection, db_files)
    for file in [db_files / "schema.sql", *(db_files / "migrations").glob("*.sql")]:
        file.write_bytes(file.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
    assert migrate(connection, db_files) == []


def test_history_gap_does_not_replay_data_migration(database):
    _, connection = database
    migrate(connection)
    connection.execute("INSERT INTO app_user(login,password_hash,must_change_password) "
                       "VALUES ('manager','hash',FALSE)")
    connection.execute("DELETE FROM schema_migration WHERE name = '002_password_policy.sql'")
    with pytest.raises(migrations.MigrationError, match="Пропуск"):
        migrate(connection)
    assert not connection.execute("SELECT must_change_password FROM app_user").fetchone()["must_change_password"]


def test_parallel_runs_are_serialized(database):
    dsn, connection = database
    locked = threading.Event()
    release = threading.Event()

    def first():
        with psycopg.connect(dsn, autocommit=True) as handle:
            handle.execute("SELECT pg_advisory_lock(%s)", (migrations.LOCK_ID,))
            locked.set()
            assert release.wait(10)
            try:
                return migrate(handle)
            finally:
                handle.execute("SELECT pg_advisory_unlock(%s)", (migrations.LOCK_ID,))

    def second():
        with psycopg.connect(dsn, autocommit=True) as handle:
            return migrate(handle)

    with ThreadPoolExecutor(max_workers=2) as pool:
        initial = pool.submit(first)
        assert locked.wait(10)
        concurrent = pool.submit(second)
        try:
            deadline = monotonic() + 5
            while monotonic() < deadline:
                waiting = connection.execute(
                    "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                ).fetchone()["n"]
                if waiting:
                    break
                sleep(0.01)
            assert waiting, "второй мигратор должен ждать блокировку первого"
        finally:
            release.set()
        assert len(initial.result(timeout=20)) == 20
        assert concurrent.result(timeout=20) == []
    assert len(history(connection)) == 20


@pytest.fixture
def load_api(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    packages = []

    def create(dsn):
        monkeypatch.setenv("DATABASE_URL", dsn)
        monkeypatch.setenv("API_SECRET", "test-only-" + "x" * 40)
        name = "migration_api_" + uuid.uuid4().hex
        packages.append(name)
        spec = importlib.util.spec_from_file_location(
            name, APP / "__init__.py", submodule_search_locations=[str(APP)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
        main = importlib.import_module(name + ".main")
        return main, TestClient(main.app)

    yield create
    for name in list(sys.modules):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in packages):
            sys.modules.pop(name)


def test_api_refuses_unmigrated_database_and_closes_pool(database, load_api):
    dsn, _ = database
    main, client = load_api(dsn)
    with pytest.raises(RuntimeError, match="истории схемы"), client:
        pass
    assert main.db.pool.closed


def test_api_serves_health_after_migrations(database, load_api):
    dsn, connection = database
    migrate(connection)
    main, client = load_api(dsn)
    security = importlib.import_module(main.__package__ + ".security")
    connection.execute("INSERT INTO app_user(login,password_hash,is_admin) VALUES ('admin',%s,TRUE)",
                       (security.hash_password("test-password-123"),))
    with client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "payments": 0}
        login = client.post("/api/auth/token", data={"username": "admin", "password": "test-password-123"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": "Bearer " + login.json()["access_token"],
                   "X-App-Version": "3.9.2", "X-App-Platform": "windows"}
        for path in ("/api/users/me", "/api/payments", "/api/claims", "/api/covers",
                     "/api/suppliers/directions", "/api/reports/profiles", "/api/dds-items"):
            response = client.get(path, headers=headers)
            assert response.status_code == 200, (path, response.text)
    assert main.db.pool.closed
