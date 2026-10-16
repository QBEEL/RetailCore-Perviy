"""Хранилище заметок: дерево страниц с текстом в Markdown.

Соединение SQLite нельзя делить между потоками, поэтому каждая функция
открывает своё и закрывает за собой — так же, как в базе поставщиков.

Набор функций намеренно узкий и не знает про Qt: когда заметки станут общими,
этот модуль можно подменить серверным по схеме `payments/data.py`, не трогая
интерфейс.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from .. import appdata
from ..normalize import normalize_text
from . import schema

DB_FILE = "notes.db"


@dataclass(frozen=True, slots=True)
class Note:
    id: int
    parent_id: int | None
    title: str
    body: str
    position: int
    created_at: str
    updated_at: str


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
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        # Без этого каскадное удаление вложенных страниц не сработает.
        connection.execute("PRAGMA foreign_keys = ON")
        schema.migrate(connection)
        yield connection
    finally:
        connection.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _note(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"], parent_id=row["parent_id"], title=row["title"], body=row["body"],
        position=row["position"], created_at=row["created_at"], updated_at=row["updated_at"],
    )


def search_key_of(title: str, body: str) -> str:
    return normalize_text(f"{title} {body}")


# --- чтение -------------------------------------------------------------------

def list_notes(path: str | None = None) -> list[Note]:
    """Все страницы: порядок — как они стоят в дереве среди соседей."""
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM note ORDER BY COALESCE(parent_id, 0), position, id").fetchall()
    return [_note(row) for row in rows]


def get_note(note_id: int, path: str | None = None) -> Note | None:
    with connect(path) as connection:
        row = connection.execute("SELECT * FROM note WHERE id = ?", (note_id,)).fetchone()
    return _note(row) if row is not None else None


def descendants(note_id: int, path: str | None = None) -> list[int]:
    """Номера всех вложенных страниц, без самой."""
    with connect(path) as connection:
        return _descendants(connection, note_id)


def _descendants(connection: sqlite3.Connection, note_id: int) -> list[int]:
    rows = connection.execute(
        """
        WITH RECURSIVE sub(id) AS (
            SELECT id FROM note WHERE parent_id = ?
            UNION ALL
            SELECT n.id FROM note n JOIN sub ON n.parent_id = sub.id
        )
        SELECT id FROM sub
        """,
        (note_id,),
    ).fetchall()
    return [row["id"] for row in rows]


def search(query: str, path: str | None = None) -> set[int]:
    """Страницы, в заголовке или тексте которых есть все слова запроса.

    Пустой запрос ничего не фильтрует, поэтому возвращает все страницы.
    """
    words = normalize_text(query).split()
    with connect(path) as connection:
        rows = connection.execute("SELECT id, search_key FROM note").fetchall()
    return {row["id"] for row in rows if all(word in row["search_key"] for word in words)}


# --- запись -------------------------------------------------------------------

def create_note(
    parent_id: int | None = None,
    title: str = "",
    body: str = "",
    path: str | None = None,
) -> Note:
    """Новая страница в конце списка среди соседей."""
    now = _now()
    with connect(path) as connection:
        if parent_id is not None and connection.execute(
                "SELECT 1 FROM note WHERE id = ?", (parent_id,)).fetchone() is None:
            raise ValueError(f"Страницы {parent_id} нет")
        last = connection.execute(
            "SELECT COALESCE(MAX(position), -1) FROM note WHERE parent_id IS ?",
            (parent_id,)).fetchone()[0]
        cursor = connection.execute(
            "INSERT INTO note (parent_id, title, body, position, search_key, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (parent_id, title, body, last + 1, search_key_of(title, body), now, now),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM note WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _note(row)


def save_note(note_id: int, title: str, body: str, path: str | None = None) -> bool:
    """Сохраняет заголовок и текст. False — если страницу уже удалили."""
    with connect(path) as connection:
        cursor = connection.execute(
            "UPDATE note SET title = ?, body = ?, search_key = ?, updated_at = ? WHERE id = ?",
            (title, body, search_key_of(title, body), _now(), note_id),
        )
        connection.commit()
    return cursor.rowcount > 0


def move_note(
    note_id: int,
    parent_id: int | None,
    position: int | None = None,
    path: str | None = None,
) -> None:
    """Переносит страницу под нового родителя на указанное место среди соседей.

    `position=None` — в конец. Перенос страницы в саму себя или в своего потомка
    запрещён: дерево превратилось бы в замкнутый круг, и страницы пропали бы из него.
    """
    with connect(path) as connection:
        if connection.execute("SELECT 1 FROM note WHERE id = ?", (note_id,)).fetchone() is None:
            raise ValueError(f"Страницы {note_id} нет")
        if parent_id is not None:
            if parent_id == note_id or parent_id in _descendants(connection, note_id):
                raise ValueError("Страницу нельзя перенести внутрь самой себя")
            if connection.execute("SELECT 1 FROM note WHERE id = ?", (parent_id,)).fetchone() is None:
                raise ValueError(f"Страницы {parent_id} нет")
        siblings = [
            row["id"] for row in connection.execute(
                "SELECT id FROM note WHERE parent_id IS ? AND id != ? ORDER BY position, id",
                (parent_id, note_id)).fetchall()
        ]
        index = len(siblings) if position is None else max(0, min(position, len(siblings)))
        siblings.insert(index, note_id)
        connection.execute("UPDATE note SET parent_id = ? WHERE id = ?", (parent_id, note_id))
        connection.executemany(
            "UPDATE note SET position = ? WHERE id = ?",
            [(order, sibling) for order, sibling in enumerate(siblings)])
        connection.commit()


def delete_note(note_id: int, path: str | None = None) -> int:
    """Удаляет страницу вместе со вложенными. Возвращает, сколько страниц ушло."""
    with connect(path) as connection:
        count = len(_descendants(connection, note_id))
        cursor = connection.execute("DELETE FROM note WHERE id = ?", (note_id,))
        connection.commit()
    return count + 1 if cursor.rowcount else 0
