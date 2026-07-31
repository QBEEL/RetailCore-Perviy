"""Пул соединений с PostgreSQL.

Пул, а не соединение на запрос: установка соединения дороже самого запроса,
а запросы здесь короткие. Размер маленький — пользователей два десятка, и
больше четырёх одновременных запросов к базе тут не бывает.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .settings import settings

pool = ConnectionPool(
    settings.dsn,
    min_size=1,
    max_size=4,
    kwargs={"row_factory": dict_row},
    open=False,
)


@contextmanager
def cursor() -> Iterator[Any]:
    """Курсор в транзакции: выход без исключения фиксирует, исключение — откат."""
    with pool.connection() as connection:
        with connection.cursor() as handle:
            yield handle


def fetch_all(query: str, params: Any = None) -> list[dict]:
    with cursor() as handle:
        handle.execute(query, params)
        return handle.fetchall()


def fetch_one(query: str, params: Any = None) -> dict | None:
    with cursor() as handle:
        handle.execute(query, params)
        return handle.fetchone()


def execute(query: str, params: Any = None) -> int:
    with cursor() as handle:
        handle.execute(query, params)
        return handle.rowcount
