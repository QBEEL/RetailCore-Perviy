"""Хранилище операций маркировки и результатов проверки кодов.

Соединение SQLite нельзя делить между потоками, поэтому каждая функция
открывает своё и закрывает за собой — так же, как в базах оплат и отчётности:
чтение идёт из потока интерфейса, а отправка уходит в фоновую задачу.

Главное правило здесь одно и оно про повторы: операцию заводят до отправки, а
не после. Запись, оставшаяся в состоянии «отправляется», означает не «не
дошло», а «неизвестно» — и разрешать по ней повтор нельзя.
"""
from __future__ import annotations

import getpass
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, Iterator, Sequence

from .. import appdata
from . import schema
from .models import (
    CodeInfo,
    Contour,
    Operation,
    OperationKind,
    OperationStatus,
    fingerprint,
    parse_state,
)

DB_FILE = "marking.db"


def database_path() -> str:
    return appdata.path_to(DB_FILE)


@contextmanager
def connect(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Открывает базу, при необходимости создавая её и приводя схему к версии."""
    target = path or database_path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    connection = sqlite3.connect(target)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        schema.migrate(connection)
        yield connection
    finally:
        connection.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _moment(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _author() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — имя пользователя не должно ломать запись
        return ""


# --- операции ---------------------------------------------------------------------

def create(operation: Operation, path: str | None = None) -> Operation:
    """Заводит операцию в состоянии черновика — до всякой отправки.

    Порядок именно такой: сначала запись, потом обращение к ГИС МТ. Оборвись
    связь на отправке, останется след, по которому видно, что операция была
    начата, и её состояние можно выяснить опросом.
    """
    if not operation.codes:
        raise ValueError("В операции нет ни одного кода")
    operation.created_at = operation.created_at or datetime.now()
    operation.author = operation.author or _author()
    with connect(path) as connection:
        cursor = connection.execute(
            "INSERT INTO operation (local_id, kind, product_group, contour, status,"
            "                       document_id, codes, codes_hash, reason, comment,"
            "                       error, author, certificate, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (operation.local_id, operation.kind.value, operation.product_group,
             operation.contour.value, operation.status.value, operation.document_id,
             "\n".join(operation.codes), fingerprint(operation.codes),
             operation.reason, operation.comment, operation.error,
             operation.author, operation.certificate,
             operation.created_at.isoformat(timespec="seconds")))
        operation.id = int(cursor.lastrowid)
        connection.commit()
    return operation


def update(operation: Operation, path: str | None = None) -> Operation:
    """Сохраняет состояние операции. Коды не меняются — они заданы при заводе."""
    if not operation.id:
        raise ValueError("Операция не заведена в базе")
    with connect(path) as connection:
        connection.execute(
            "UPDATE operation SET status = ?, document_id = ?, error = ?,"
            "                     reason = ?, comment = ?, certificate = ?,"
            "                     sent_at = ?, checked_at = ?"
            " WHERE id = ?",
            (operation.status.value, operation.document_id, operation.error,
             operation.reason, operation.comment, operation.certificate,
             operation.sent_at.isoformat(timespec="seconds") if operation.sent_at else "",
             operation.checked_at.isoformat(timespec="seconds") if operation.checked_at else "",
             operation.id))
        connection.commit()
    return operation


def _operation(row: sqlite3.Row) -> Operation:
    return Operation(
        id=int(row["id"]),
        local_id=row["local_id"],
        kind=_kind(row["kind"]),
        product_group=row["product_group"] or "",
        contour=_contour(row["contour"]),
        status=_status(row["status"]),
        document_id=row["document_id"] or "",
        codes=[line for line in (row["codes"] or "").split("\n") if line],
        reason=row["reason"] or "",
        comment=row["comment"] or "",
        error=row["error"] or "",
        author=row["author"] or "",
        certificate=row["certificate"] or "",
        created_at=_moment(row["created_at"]),
        sent_at=_moment(row["sent_at"]),
        checked_at=_moment(row["checked_at"]),
    )


def _kind(value: object) -> OperationKind:
    try:
        return OperationKind(str(value))
    except ValueError:
        return OperationKind.CHECK


def _status(value: object) -> OperationStatus:
    try:
        return OperationStatus(str(value))
    except ValueError:
        # Неизвестное состояние безопаснее считать незавершённым: так операция
        # попадёт в опрос, а не будет принята за выполненную.
        return OperationStatus.SENT


def _contour(value: object) -> Contour:
    try:
        return Contour(str(value))
    except ValueError:
        return Contour.SANDBOX


def get(operation_id: int, path: str | None = None) -> Operation | None:
    with connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM operation WHERE id = ?", (operation_id,)).fetchone()
    return _operation(row) if row else None


def by_local_id(local_id: str, path: str | None = None) -> Operation | None:
    with connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM operation WHERE local_id = ?", (local_id,)).fetchone()
    return _operation(row) if row else None


def recent(limit: int = 200, path: str | None = None) -> list[Operation]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM operation ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,)).fetchall()
    return [_operation(row) for row in rows]


def pending(path: str | None = None) -> list[Operation]:
    """Операции, ждущие ответа ГИС МТ.

    Их опрашивают при открытии вкладки: приложение могли закрыть сразу после
    отправки, и результат остался невыясненным.
    """
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM operation WHERE status IN ('sending', 'sent')"
            " ORDER BY created_at",
            ).fetchall()
    return [_operation(row) for row in rows]


def duplicates(codes: Iterable[str], kind: OperationKind,
               path: str | None = None) -> list[Operation]:
    """Операции того же вида с тем же набором кодов.

    Проверяется перед отправкой. Совпадение не запрещает действие само по себе
    — товар и правда могли принять дважды разными поставками, — но показать
    его пользователю обязательно: чаще всего это второе нажатие, а не второй
    случай.
    """
    mark = fingerprint(codes)
    if not mark:
        return []
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM operation WHERE codes_hash = ? AND kind = ?"
            "   AND status NOT IN ('draft', 'failed', 'rejected')"
            " ORDER BY created_at DESC",
            (mark, kind.value)).fetchall()
    return [_operation(row) for row in rows]


def delete(operation_id: int, path: str | None = None) -> None:
    """Удаляет операцию. Разрешено только для того, что заведомо не ушло.

    Отправленную удалять нельзя ни при каких условиях: запись — единственное
    свидетельство того, что документ в ГИС МТ существует.
    """
    with connect(path) as connection:
        row = connection.execute(
            "SELECT status FROM operation WHERE id = ?", (operation_id,)).fetchone()
        if row is None:
            return
        if not _status(row["status"]).resendable:
            raise ValueError(
                "Операцию, отправленную в систему маркировки, удалить нельзя — "
                "это единственный след того, что документ существует.")
        connection.execute("DELETE FROM operation WHERE id = ?", (operation_id,))
        connection.commit()


# --- результаты проверки кодов ------------------------------------------------------

def remember_checks(items: Sequence[CodeInfo], contour: Contour,
                    path: str | None = None) -> None:
    """Складывает ответы проверки. Повторная проверка кода переписывает запись."""
    if not items:
        return
    now = _now()
    with connect(path) as connection:
        connection.executemany(
            "INSERT INTO code_check (code, gtin, serial, state, valid, found,"
            "                        owner_inn, owner_name, product_name, contour,"
            "                        checked_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(code) DO UPDATE SET"
            "   gtin = excluded.gtin, serial = excluded.serial,"
            "   state = excluded.state, valid = excluded.valid,"
            "   found = excluded.found, owner_inn = excluded.owner_inn,"
            "   owner_name = excluded.owner_name,"
            "   product_name = excluded.product_name,"
            "   contour = excluded.contour, checked_at = excluded.checked_at",
            [(item.code, item.gtin, item.serial, item.state.value,
              int(item.valid), int(item.found), item.owner_inn, item.owner_name,
              item.product_name, contour.value, now) for item in items])
        connection.commit()


def known_checks(codes: Sequence[str], contour: Contour,
                 path: str | None = None) -> dict[str, CodeInfo]:
    """Ранее полученные ответы по этим кодам — из того же контура.

    Контур учитывается намеренно: один и тот же код в песочнице и в бою — это
    разные вещи, и смешивать их ответы нельзя.
    """
    if not codes:
        return {}
    result: dict[str, CodeInfo] = {}
    with connect(path) as connection:
        # Порциями: SQLite ограничивает число параметров запроса, а кодов в
        # приёмке бывают тысячи.
        chunk = 500
        for start in range(0, len(codes), chunk):
            part = list(codes[start:start + chunk])
            marks = ",".join("?" for _ in part)
            rows = connection.execute(
                f"SELECT * FROM code_check WHERE contour = ? AND code IN ({marks})",
                (contour.value, *part)).fetchall()
            for row in rows:
                result[row["code"]] = CodeInfo(
                    code=row["code"],
                    gtin=row["gtin"] or "",
                    serial=row["serial"] or "",
                    state=parse_state(row["state"]),
                    valid=bool(row["valid"]),
                    found=bool(row["found"]),
                    owner_inn=row["owner_inn"] or "",
                    owner_name=row["owner_name"] or "",
                    product_name=row["product_name"] or "",
                )
    return result


def forget_checks(path: str | None = None) -> int:
    """Очищает запомненные ответы. Состояние кода меняется, и кэш устаревает."""
    with connect(path) as connection:
        cursor = connection.execute("DELETE FROM code_check")
        connection.commit()
        return cursor.rowcount
