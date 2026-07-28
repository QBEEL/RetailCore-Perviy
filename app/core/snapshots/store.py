"""Хранилище снимков: создание, чтение и удаление версий каталога.

Соединение SQLite нельзя использовать из нескольких потоков, поэтому каждая
функция открывает своё и закрывает его за собой — снимок создаётся в рабочем
потоке (`app/ui/tasks.py`), а список читается в потоке интерфейса.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import sqlite3
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime
from typing import Any, Callable, Iterator, Sequence

from .. import appdata
from ..models import FieldRole, Record, Sheet
from . import schema
from .models import Snapshot, SnapshotProduct

LOG_FILE = "snapshot.log"
DB_FILE = "snapshots.db"

ProgressCallback = Callable[[int, int], None]

# Строк на один executemany: пачками вставка идёт в разы быстрее построчной,
# но пачка целиком лежит в памяти — 1000 строк это компромисс.
_BATCH = 1000

# Роль → колонка таблицы product. Порядок совпадает с _INSERT.
_COLUMNS: tuple[tuple[str, FieldRole], ...] = (
    ("article", FieldRole.ARTICLE),
    ("ean", FieldRole.EAN),
    ("sku", FieldRole.SKU),
    ("name", FieldRole.NAME),
    ("brand", FieldRole.BRAND),
    ("category", FieldRole.CATEGORY),
    ("volume", FieldRole.VOLUME),
)

_INSERT = """
INSERT INTO product (snapshot_id, row, article, ean, sku, name, brand,
                     category, volume, price, match_key, payload)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def database_path() -> str:
    return appdata.path_to(DB_FILE)


@contextmanager
def connect(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Открывает базу, при необходимости создавая её и приводя схему к версии."""
    target = path or database_path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    connection = sqlite3.connect(target)
    try:
        connection.row_factory = sqlite3.Row
        # Освобождённые страницы возвращаются файлу порциями: иначе после
        # удаления снимка база не уменьшается, и чистка не даёт места.
        # Режим задаётся до создания таблиц — на пустом файле он применяется сразу.
        connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
        # WAL не блокирует чтение во время записи снимка; foreign_keys нужен,
        # чтобы удаление снимка забирало с собой товары.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA foreign_keys = ON")
        schema.migrate(connection)
        yield connection
    finally:
        connection.close()


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create(
    sheet: Sheet,
    path: str | None = None,
    description: str = "",
    progress: ProgressCallback | None = None,
) -> Snapshot | None:
    """Сохраняет состояние листа. Возвращает None, если такой снимок уже есть.

    Повтор определяется по хешу содержимого файла: переключение вкладок и
    повторный выбор того же файла не должны плодить одинаковые версии.
    """
    digest = file_hash(sheet.path)
    records = sheet.records
    total = len(records)

    with connect(path) as connection:
        existing = connection.execute(
            "SELECT id FROM snapshot WHERE source_file_hash = ? AND sheet_name = ?",
            (digest, sheet.sheet_name),
        ).fetchone()
        if existing is not None:
            _log(f"Import started\nFile: {os.path.basename(sheet.path)}\n"
                 f"Products: {total}\nSnapshot skipped: identical content, ID={existing['id']}\n"
                 f"Status: SKIPPED", path)
            return None

        cursor = connection.execute(
            """INSERT INTO snapshot (created_at, source_file_name, source_file_path,
                                     source_file_hash, sheet_name, total_products,
                                     brand, category, user_id, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().isoformat(timespec="seconds"), os.path.basename(sheet.path),
             sheet.path, digest, sheet.sheet_name, total,
             _dominant(records, FieldRole.BRAND), _dominant(records, FieldRole.CATEGORY),
             _user(), description),
        )
        snapshot_id = int(cursor.lastrowid)

        for done in _insert_products(connection, snapshot_id, records):
            if progress:
                progress(done, total)
        connection.commit()

        row = connection.execute("SELECT * FROM snapshot WHERE id = ?", (snapshot_id,)).fetchone()

    _log(f"Import started\nFile: {os.path.basename(sheet.path)}\n"
         f"Products: {total}\nSnapshot created: ID={snapshot_id}\nStatus: SUCCESS", path)
    return _snapshot(row)


def _insert_products(
    connection: sqlite3.Connection, snapshot_id: int, records: Sequence[Record]
) -> Iterator[int]:
    """Вставляет товары пачками, отдавая число уже записанных строк."""
    batch: list[tuple[Any, ...]] = []
    for done, record in enumerate(records, start=1):
        batch.append(_row(snapshot_id, record))
        if len(batch) >= _BATCH:
            connection.executemany(_INSERT, batch)
            batch.clear()
            yield done
    if batch:
        connection.executemany(_INSERT, batch)
    yield len(records)


def _row(snapshot_id: int, record: Record) -> tuple[Any, ...]:
    values = [record.text(role) for _, role in _COLUMNS]
    return (
        snapshot_id,
        record.row,
        *values,
        _price(record.get(FieldRole.PRICE)),
        record.match_key,
        json.dumps(record.values, ensure_ascii=False, default=str),
    )


def list_snapshots(path: str | None = None) -> list[Snapshot]:
    with connect(path) as connection:
        rows = connection.execute("SELECT * FROM snapshot ORDER BY created_at DESC, id DESC").fetchall()
    return [_snapshot(row) for row in rows]


def get(snapshot_id: int, path: str | None = None) -> Snapshot | None:
    with connect(path) as connection:
        row = connection.execute("SELECT * FROM snapshot WHERE id = ?", (snapshot_id,)).fetchone()
    return _snapshot(row) if row is not None else None


def products(snapshot_id: int, path: str | None = None) -> list[SnapshotProduct]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM product WHERE snapshot_id = ? ORDER BY row", (snapshot_id,)
        ).fetchall()
    return [_product(row) for row in rows]


def delete(snapshot_id: int, path: str | None = None) -> bool:
    with connect(path) as connection:
        cursor = connection.execute("DELETE FROM snapshot WHERE id = ?", (snapshot_id,))
        connection.commit()
        removed = cursor.rowcount > 0
        if removed:
            _reclaim(connection)
    if removed:
        _log(f"Snapshot deleted: ID={snapshot_id}\nStatus: SUCCESS", path)
    return removed


def _reclaim(connection: sqlite3.Connection) -> None:
    """Возвращает освобождённое место файлу базы.

    Снимок каталога на 100 тыс. товаров занимает ~28 МБ, и без этого шага файл
    не уменьшался бы: пользователь чистит историю, а место не освобождается.
    """
    if connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2:  # INCREMENTAL
        # fetchall обязателен: incremental_vacuum выполняется пошагово, и без
        # вычитывания результата освобождает ноль страниц.
        connection.execute("PRAGMA incremental_vacuum").fetchall()
        connection.commit()
    else:
        # База создана до включения auto_vacuum — только полная перезапись.
        connection.commit()
        connection.execute("VACUUM")
    # Страницы освобождены в WAL; без слияния файл на диске так и не уменьшится.
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def database_size(path: str | None = None) -> int:
    """Размер базы вместе с журналом WAL, который тоже занимает место."""
    target = path or database_path()
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(target + suffix)
        except OSError:
            pass
    return total


# --- преобразования ------------------------------------------------------------

def _snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        id=row["id"],
        created_at=_moment(row["created_at"]),
        source_file_name=row["source_file_name"],
        source_file_path=row["source_file_path"],
        source_file_hash=row["source_file_hash"],
        sheet_name=row["sheet_name"],
        total_products=row["total_products"],
        brand=row["brand"],
        category=row["category"],
        user_id=row["user_id"],
        description=row["description"],
    )


def _product(row: sqlite3.Row) -> SnapshotProduct:
    try:
        values = json.loads(row["payload"])
    except (ValueError, TypeError):
        values = []
    return SnapshotProduct(
        row=row["row"],
        article=row["article"],
        ean=row["ean"],
        sku=row["sku"],
        name=row["name"],
        brand=row["brand"],
        category=row["category"],
        volume=row["volume"],
        price=row["price"],
        match_key=row["match_key"],
        values=values,
    )


def _moment(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return datetime.min


def _price(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _dominant(records: Sequence[Record], role: FieldRole) -> str:
    """Описание роли одной строкой: имя, если файл про один бренд, иначе счёт.

    У дистрибьютора в прайсе десятки брендов, и «самый частый» из них
    подписывал бы файл именем случайного бренда — в истории это выглядело бы
    так, будто весь прайс принадлежит ему.
    """
    counter = Counter(text for record in records if (text := record.text(role)))
    if not counter:
        return ""
    top, count = counter.most_common(1)[0]
    if len(counter) == 1:
        return top
    total = sum(counter.values())
    if count / total >= 0.8:
        return f"{top} и ещё {_plural(len(counter) - 1, 'бренд', 'бренда', 'брендов')}"
    return _plural(len(counter), "бренд", "бренда", "брендов")


def _plural(count: int, one: str, few: str, many: str) -> str:
    tail, hundred = count % 10, count % 100
    if tail == 1 and hundred != 11:
        word = one
    elif 2 <= tail <= 4 and not 12 <= hundred <= 14:
        word = few
    else:
        word = many
    return f"{count} {word}"


def is_trackable(sheet: Sheet) -> bool:
    """Стоит ли вести историю по этому листу.

    В книгах поставщиков первым идёт лист-обложка («Главная», бланк для
    заполнения), и снимок с него засорял бы историю строками вроде «ФИО
    получателя». Лист считается каталогом, если у заметной доли строк есть
    идентификатор товара либо цена.
    """
    total = len(sheet.records)
    if total < 5:
        return False
    identifiers = sum(
        1 for record in sheet.records
        if any(record.by_role.get(role) is not None
               for role in (FieldRole.ARTICLE, FieldRole.EAN, FieldRole.SKU))
    )
    if identifiers / total >= 0.25:
        return True
    prices = sum(1 for record in sheet.records if record.by_role.get(FieldRole.PRICE) is not None)
    return total >= 20 and prices / total >= 0.5


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — имя пользователя не критично для снимка
        return ""


def _log(message: str, path: str | None = None) -> None:
    """Журнал живёт рядом со своей базой.

    Иначе работа с временной базой (тесты, диагностика `--selftest`) писала бы
    в журнал пользователя записи о снимках, которых нет в его истории.
    """
    if path is None:
        appdata.log_event(LOG_FILE, message)
        return
    destination = os.path.join(os.path.dirname(path) or ".", LOG_FILE)
    try:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M}\n{message}\n\n")
    except OSError:
        pass
