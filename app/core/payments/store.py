"""Хранилище оплат: платежи, бюджеты, привязки получателей, журнал импортов.

Соединение SQLite нельзя делить между потоками, поэтому каждая функция
открывает своё и закрывает за собой — так же, как в базе поставщиков: чтение
идёт из потока интерфейса, а импорт и пересчёт уходят в фоновую задачу.

Выборка возвращается целиком, а не постранично. Вся история — около семи тысяч
строк, это десятки миллисекунд и единицы мегабайт; постраничное чтение здесь
усложнило бы сортировку и массовые операции, ничего не дав взамен.
"""
from __future__ import annotations

import getpass
import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Iterator, Sequence

from .. import appdata
from .models import (
    Budget,
    Payment,
    PaymentFile,
    PaymentOrigin,
    PaymentStatus,
    SUPPLIER_OPERATION,
    SupplierRow,
)
from . import schema
from .recipients import recipient_key

DB_FILE = "payments.db"
FILES_DIR = "payment_files"

# Поля, которые приходят из выгрузки 1С и потому обновляются при повторном
# импорте. Всё остальное — комментарий, вложения, перенос даты — принадлежит
# пользователю, и импорт их не касается.
IMPORTED_FIELDS: tuple[str, ...] = (
    "pay_date", "amount", "vat", "currency", "recipient", "recipient_key",
    "source_status", "paid_flag", "operation", "over_limit", "priority",
    "edo_state", "responsible", "author", "had_files",
)


def database_path() -> str:
    return appdata.path_to(DB_FILE)


def files_dir() -> str:
    return appdata.path_to(FILES_DIR)


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
        connection.execute("PRAGMA foreign_keys = ON")
        schema.migrate(connection)
        yield connection
    finally:
        connection.close()


# --- отбор ---------------------------------------------------------------------

@dataclass(slots=True)
class Filter:
    """Условия отбора платежей. Пустые поля не ограничивают выборку."""

    text: str = ""
    start: date | None = None
    end: date | None = None
    statuses: tuple[PaymentStatus, ...] = ()
    supplier_id: int = 0
    recipient: str = ""
    amount_from: float | None = None
    amount_to: float | None = None
    responsible: str = ""
    operation: str = ""
    over_limit: bool | None = None
    suppliers_only: bool = False
    # Платежи без даты в календарь не попадают, а в таблице должны быть видны.
    dated_only: bool = False

    @property
    def active(self) -> bool:
        return bool(
            self.text or self.start or self.end or self.statuses or self.supplier_id
            or self.recipient or self.amount_from is not None or self.amount_to is not None
            or self.responsible or self.operation or self.over_limit is not None
            or self.suppliers_only
        )

    def where(self) -> tuple[str, list[Any]]:
        """Собирает условие и параметры. Подстановка только через параметры."""
        parts: list[str] = []
        values: list[Any] = []
        if self.text:
            like = f"%{self.text.strip()}%"
            parts.append(
                "(recipient LIKE ? OR doc_number LIKE ? OR comment LIKE ?"
                " OR responsible LIKE ? OR author LIKE ?)")
            values.extend([like] * 5)
        if self.start:
            parts.append("pay_date <> '' AND pay_date >= ?")
            values.append(self.start.isoformat())
        if self.end:
            parts.append("pay_date <> '' AND pay_date <= ?")
            values.append(self.end.isoformat())
        if self.dated_only:
            parts.append("pay_date <> ''")
        if self.statuses:
            marks = ", ".join("?" for _ in self.statuses)
            parts.append(f"status IN ({marks})")
            values.extend(status.value for status in self.statuses)
        if self.supplier_id:
            parts.append("supplier_id = ?")
            values.append(self.supplier_id)
        if self.recipient:
            parts.append("recipient_key = ?")
            values.append(recipient_key(self.recipient))
        if self.amount_from is not None:
            parts.append("amount >= ?")
            values.append(self.amount_from)
        if self.amount_to is not None:
            parts.append("amount <= ?")
            values.append(self.amount_to)
        if self.responsible:
            parts.append("responsible = ?")
            values.append(self.responsible)
        if self.operation:
            parts.append("operation = ?")
            values.append(self.operation)
        if self.over_limit is not None:
            parts.append("over_limit = ?")
            values.append(int(self.over_limit))
        if self.suppliers_only:
            parts.append("operation = ?")
            values.append(SUPPLIER_OPERATION)
        return (" AND ".join(f"({p})" for p in parts) if parts else "1"), values


# --- платежи -------------------------------------------------------------------

def list_payments(
    selection: Filter | None = None,
    path: str | None = None,
    *,
    order: str = "pay_date DESC, id DESC",
    limit: int = 0,
) -> list[Payment]:
    condition, values = (selection or Filter()).where()
    query = (
        "SELECT p.*, (SELECT COUNT(*) FROM payment_file f WHERE f.payment_id = p.id) AS files"
        f" FROM payment p WHERE {condition} ORDER BY {order}"
    )
    if limit:
        query += f" LIMIT {int(limit)}"
    with connect(path) as connection:
        rows = connection.execute(query, values).fetchall()
    return [_payment(row) for row in rows]


def get_payment(payment_id: int, path: str | None = None) -> Payment | None:
    with connect(path) as connection:
        row = connection.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM payment_file f WHERE f.payment_id = p.id) AS files"
            " FROM payment p WHERE p.id = ?", (payment_id,)).fetchone()
    return _payment(row) if row is not None else None


def save_payment(payment: Payment, path: str | None = None) -> Payment:
    """Создаёт или обновляет платёж."""
    if payment.amount <= 0:
        raise ValueError("Сумма оплаты должна быть больше нуля")
    if not (payment.recipient.strip() or payment.supplier_id):
        raise ValueError("У оплаты должен быть получатель")
    now = _now()
    values = _values(payment)
    with connect(path) as connection:
        if payment.id:
            assignments = ", ".join(f"{name} = ?" for name in values)
            connection.execute(
                f"UPDATE payment SET {assignments}, updated_at = ? WHERE id = ?",
                [*values.values(), now, payment.id])
        else:
            names = ", ".join([*values, "created_at", "updated_at"])
            marks = ", ".join("?" for _ in range(len(values) + 2))
            cursor = connection.execute(
                f"INSERT INTO payment ({names}) VALUES ({marks})",
                [*values.values(), now, now])
            payment.id = int(cursor.lastrowid)
        connection.commit()
    return payment


def delete_payment(payment_id: int, path: str | None = None) -> bool:
    """Удаляет платёж вместе с вложениями."""
    for attachment in files(payment_id, path):
        _drop_file(attachment.path)
    with connect(path) as connection:
        cursor = connection.execute("DELETE FROM payment WHERE id = ?", (payment_id,))
        connection.commit()
        return cursor.rowcount > 0


def update_many(
    ids: Sequence[int],
    path: str | None = None,
    *,
    status: PaymentStatus | None = None,
    pay_date: date | None = None,
    responsible: str | None = None,
    supplier_id: int | None = None,
) -> int:
    """Массовое изменение выделенных строк. Пустые параметры не трогаются."""
    if not ids:
        return 0
    assignments: list[str] = []
    values: list[Any] = []
    if status is not None:
        assignments.append("status = ?")
        values.append(status.value)
        # Отметка об оплате должна следовать за статусом, иначе отчёты разойдутся.
        assignments.append("paid_flag = ?")
        values.append(int(status is PaymentStatus.PAID))
    if pay_date is not None:
        assignments.append("pay_date = ?")
        values.append(pay_date.isoformat())
    if responsible is not None:
        assignments.append("responsible = ?")
        values.append(responsible)
    if supplier_id is not None:
        assignments.append("supplier_id = ?")
        values.append(supplier_id)
    if not assignments:
        return 0
    marks = ", ".join("?" for _ in ids)
    with connect(path) as connection:
        cursor = connection.execute(
            f"UPDATE payment SET {', '.join(assignments)}, updated_at = ?"
            f" WHERE id IN ({marks})",
            [*values, _now(), *ids])
        connection.commit()
        return cursor.rowcount


def refresh_overdue(today: date | None = None, path: str | None = None) -> int:
    """Переводит запланированное с ушедшей датой в просрочку.

    Затрагивается только «Запланировано». Оплаченное и отменённое не
    пересчитывается никогда: отклонённые заявки прошлых лет иначе стали бы
    просрочкой и повисли вечным долгом. «Перенесено» тоже не трогаем — у
    переноса есть новая дата, назначенная человеком.
    """
    limit = (today or date.today()).isoformat()
    with connect(path) as connection:
        cursor = connection.execute(
            "UPDATE payment SET status = ?, updated_at = ?"
            " WHERE status = ? AND pay_date <> '' AND pay_date < ?",
            (PaymentStatus.OVERDUE.value, _now(), PaymentStatus.PLANNED.value, limit))
        connection.commit()
        return cursor.rowcount


def known_values(path: str | None = None) -> dict[str, list[str]]:
    """Значения для выпадающих списков: получатели, ответственные, операции."""
    queries = {
        "recipients": "SELECT recipient, COUNT(*) c FROM payment WHERE recipient <> ''"
                      " GROUP BY recipient_key ORDER BY c DESC, recipient",
        "responsible": "SELECT responsible, COUNT(*) c FROM payment WHERE responsible <> ''"
                       " GROUP BY responsible ORDER BY c DESC, responsible",
        "operations": "SELECT operation, COUNT(*) c FROM payment WHERE operation <> ''"
                      " GROUP BY operation ORDER BY c DESC, operation",
    }
    found: dict[str, list[str]] = {}
    with connect(path) as connection:
        for name, query in queries.items():
            found[name] = [row[0] for row in connection.execute(query).fetchall()]
    return found


def count_payments(path: str | None = None) -> int:
    with connect(path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM payment").fetchone()[0])


# --- импорт --------------------------------------------------------------------

@dataclass(slots=True)
class Existing:
    """Снимок записи в базе — чтобы понять, что изменилось, до записи."""

    id: int
    origin: str
    values: dict[str, Any] = field(default_factory=dict)
    status: str = ""
    manual: bool = False


def existing_index(path: str | None = None) -> dict[tuple[str, str], Existing]:
    """Записи из выгрузок, разложенные по ключу «номер + дата заявки».

    Нужны, чтобы посчитать новых и изменившихся до всякой записи в базу:
    предпросмотр импорта обещает пользователю точные числа.
    """
    names = ", ".join(IMPORTED_FIELDS)
    with connect(path) as connection:
        rows = connection.execute(
            f"SELECT id, doc_number, request_date, origin, status, comment, {names}"
            " FROM payment WHERE doc_number <> ''").fetchall()
    index: dict[tuple[str, str], Existing] = {}
    for row in rows:
        index[(row["doc_number"], row["request_date"])] = Existing(
            id=int(row["id"]),
            origin=row["origin"],
            values={name: row[name] for name in IMPORTED_FIELDS},
            status=row["status"],
            manual=bool(row["comment"]),
        )
    return index


def apply_import(
    created: Iterable[Payment],
    changed: Iterable[tuple[int, Payment]],
    path: str | None = None,
) -> tuple[int, int]:
    """Записывает разобранную выгрузку одной транзакцией.

    Изменившимся обновляются только поля из 1С: комментарий, вложения и
    назначенный вручную статус остаются на месте.
    """
    now = _now()
    new_rows = [_values(payment) for payment in created]
    updates = [(payment_id, _values(payment, imported_only=True)) for payment_id, payment in changed]
    with connect(path) as connection:
        if new_rows:
            names = ", ".join([*new_rows[0], "created_at", "updated_at"])
            marks = ", ".join("?" for _ in range(len(new_rows[0]) + 2))
            connection.executemany(
                f"INSERT INTO payment ({names}) VALUES ({marks})",
                [[*row.values(), now, now] for row in new_rows])
        if updates:
            assignments = ", ".join(f"{name} = ?" for name in updates[0][1])
            connection.executemany(
                f"UPDATE payment SET {assignments}, updated_at = ? WHERE id = ?",
                [[*row.values(), now, payment_id] for payment_id, row in updates])
        connection.commit()
    return len(new_rows), len(updates)


def log_import(
    path_to_file: str,
    file_hash: str,
    rows: int,
    created: int,
    changed: int,
    same: int,
    skipped: int,
    error: str = "",
    path: str | None = None,
) -> None:
    with connect(path) as connection:
        connection.execute(
            """INSERT INTO import_run (path, file_hash, rows_total, rows_new, rows_updated,
                                       rows_same, rows_skipped, error, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (path_to_file, file_hash, rows, created, changed, same, skipped, error, _now()))
        connection.commit()


def imported_before(file_hash: str, path: str | None = None) -> datetime | None:
    """Когда этот же файл уже заливали. Пустой хеш ничего не значит."""
    if not file_hash:
        return None
    with connect(path) as connection:
        row = connection.execute(
            "SELECT finished_at FROM import_run WHERE file_hash = ? AND error = ''"
            " ORDER BY id DESC LIMIT 1", (file_hash,)).fetchone()
    return _moment(row["finished_at"]) if row is not None else None


def last_import(path: str | None = None) -> dict[str, Any] | None:
    with connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM import_run ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row is not None else None


# --- бюджеты -------------------------------------------------------------------

def budgets(path: str | None = None) -> list[Budget]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM budget ORDER BY year DESC, month DESC").fetchall()
    return [_budget(row) for row in rows]


def get_budget(year: int, month: int, path: str | None = None) -> Budget | None:
    with connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM budget WHERE year = ? AND month = ?", (year, month)).fetchone()
    return _budget(row) if row is not None else None


def save_budget(budget: Budget, path: str | None = None) -> Budget:
    if not 1 <= budget.month <= 12:
        raise ValueError("Месяц бюджета указан неверно")
    with connect(path) as connection:
        connection.execute(
            """INSERT INTO budget (year, month, amount, note, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(year, month) DO UPDATE SET
                   amount = excluded.amount,
                   note = excluded.note,
                   updated_at = excluded.updated_at""",
            (budget.year, budget.month, budget.amount, budget.note, _now()))
        connection.commit()
    return budget


def delete_budget(year: int, month: int, path: str | None = None) -> bool:
    with connect(path) as connection:
        cursor = connection.execute(
            "DELETE FROM budget WHERE year = ? AND month = ?", (year, month))
        connection.commit()
        return cursor.rowcount > 0


# --- привязка получателей ------------------------------------------------------

def recipient_links(path: str | None = None) -> dict[str, int]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT recipient_key, supplier_id FROM recipient_link").fetchall()
    return {row["recipient_key"]: int(row["supplier_id"]) for row in rows}


def save_recipient_link(
    recipient: str,
    supplier_id: int,
    path: str | None = None,
    *,
    manual: bool = True,
) -> int:
    """Связывает получателя из 1С с карточкой поставщика и переносит связь на платежи."""
    key = recipient_key(recipient)
    if not key:
        raise ValueError("Получателя нечем опознать")
    with connect(path) as connection:
        connection.execute(
            """INSERT INTO recipient_link (recipient_key, recipient, supplier_id,
                                           linked_by, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(recipient_key) DO UPDATE SET
                   recipient = excluded.recipient,
                   supplier_id = excluded.supplier_id,
                   linked_by = excluded.linked_by,
                   updated_at = excluded.updated_at""",
            (key, recipient.strip(), supplier_id, "manual" if manual else "auto", _now()))
        cursor = connection.execute(
            "UPDATE payment SET supplier_id = ? WHERE recipient_key = ?", (supplier_id, key))
        connection.commit()
        return cursor.rowcount


def drop_recipient_link(recipient: str, path: str | None = None) -> bool:
    key = recipient_key(recipient)
    with connect(path) as connection:
        cursor = connection.execute(
            "DELETE FROM recipient_link WHERE recipient_key = ?", (key,))
        connection.execute("UPDATE payment SET supplier_id = 0 WHERE recipient_key = ?", (key,))
        connection.commit()
        return cursor.rowcount > 0


def unlinked_recipients(path: str | None = None) -> list[tuple[str, int, float]]:
    """Получатели без карточки поставщика — от крупных к мелким."""
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT recipient, COUNT(*) c, SUM(amount) s FROM payment"
            " WHERE supplier_id = 0 AND recipient <> ''"
            " GROUP BY recipient_key ORDER BY s DESC").fetchall()
    return [(row["recipient"], int(row["c"]), float(row["s"] or 0.0)) for row in rows]


# --- вложения ------------------------------------------------------------------

def files(payment_id: int, path: str | None = None) -> list[PaymentFile]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM payment_file WHERE payment_id = ? ORDER BY id", (payment_id,)).fetchall()
    return [_file(row) for row in rows]


def attach_file(payment_id: int, source: str, path: str | None = None) -> PaymentFile:
    """Копирует документ в папку профиля и запоминает за платежом.

    Исходник остаётся у пользователя, но ссылаться на него нельзя: письма
    перекладывают, а вложение платежа должно открываться и через год.
    """
    if not os.path.isfile(source):
        raise ValueError(f"Файл не найден: {source}")
    folder = os.path.join(files_dir(), str(payment_id))
    os.makedirs(folder, exist_ok=True)
    name = os.path.basename(source)
    target = os.path.join(folder, name)
    stem, suffix = os.path.splitext(name)
    attempt = 1
    while os.path.exists(target):
        attempt += 1
        target = os.path.join(folder, f"{stem} ({attempt}){suffix}")
    shutil.copy2(source, target)
    with connect(path) as connection:
        cursor = connection.execute(
            "INSERT INTO payment_file (payment_id, name, path, size, added_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (payment_id, os.path.basename(target), target, os.path.getsize(target), _now()))
        connection.commit()
        attachment_id = int(cursor.lastrowid)
    return PaymentFile(
        id=attachment_id, payment_id=payment_id, name=os.path.basename(target),
        path=target, size=os.path.getsize(target), added_at=datetime.now())


def detach_file(file_id: int, path: str | None = None) -> bool:
    with connect(path) as connection:
        row = connection.execute(
            "SELECT path FROM payment_file WHERE id = ?", (file_id,)).fetchone()
        cursor = connection.execute("DELETE FROM payment_file WHERE id = ?", (file_id,))
        connection.commit()
        removed = cursor.rowcount > 0
    if row is not None:
        _drop_file(row["path"])
    return removed


def suppliers(responsible: str = "", months: int = 0,
              path: str | None = None) -> list[SupplierRow]:
    """Поставщики из оплат с их менеджерами. Тот же ответ, что даёт сервер."""
    where: list[str] = ["recipient <> ''"]
    values: list[Any] = []
    if months > 0:
        where.append("pay_date >= date('now', ?)")
        values.append(f"-{int(months)} months")
    if responsible:
        # Отбор оставляет получателя целиком, а не только его оплаты: иначе
        # «всего оплат» у своего поставщика оказалось бы меньше настоящего.
        where.append(
            "recipient_key IN (SELECT recipient_key FROM payment"
            " WHERE responsible = ? AND recipient <> '')")
        values.append(responsible)
    condition = " AND ".join(where)

    with connect(path) as connection:
        rows = connection.execute(
            "SELECT recipient_key, MAX(recipient) recipient,"
            "       MAX(supplier_id) supplier_id, COUNT(*) payments,"
            "       SUM(amount) amount, MAX(pay_date) last_pay"
            f" FROM payment WHERE {condition}"
            " GROUP BY recipient_key ORDER BY SUM(amount) DESC",
            values).fetchall()
        managers: dict[str, list[str]] = {}
        for row in connection.execute(
                "SELECT recipient_key, responsible, COUNT(*) n FROM payment"
                f" WHERE {condition} AND responsible <> ''"
                " GROUP BY recipient_key, responsible"
                " ORDER BY n DESC, responsible", values).fetchall():
            managers.setdefault(row["recipient_key"], []).append(row["responsible"])

    return [
        SupplierRow(
            recipient_key=row["recipient_key"], recipient=row["recipient"],
            supplier_id=int(row["supplier_id"] or 0),
            payments=int(row["payments"]), amount=float(row["amount"] or 0.0),
            last_pay=_date(row["last_pay"]),
            managers=managers.get(row["recipient_key"], []),
        )
        for row in rows
    ]


def may_edit(payment: Payment | int) -> bool:
    """В своей базе править можно всё: она одна и принадлежит одному человеку.

    Функция существует ради общего с `remote` набора имён — интерфейс
    спрашивает про права одинаково, независимо от того, откуда пришли данные.
    """
    return True


def file_available(attachment: PaymentFile) -> bool:
    """Есть ли файл на месте. У локального вложения это вопрос к диску."""
    return attachment.exists


def open_path(attachment: PaymentFile) -> str:
    """Путь, по которому вложение можно открыть. Локально — он же и есть."""
    return attachment.path


def database_size(path: str | None = None) -> int:
    target = path or database_path()
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(target + suffix)
        except OSError:
            pass
    return total


# --- преобразования ------------------------------------------------------------

def imported_values(payment: Payment) -> dict[str, Any]:
    """Только те поля, что приходят из выгрузки 1С — для сравнения при импорте."""
    return _values(payment, imported_only=True)


def _values(payment: Payment, *, imported_only: bool = False) -> dict[str, Any]:
    """Поля платежа для записи. `imported_only` — только пришедшее из 1С."""
    everything: dict[str, Any] = {
        "doc_number": payment.doc_number,
        "request_date": _text_date(payment.request_date),
        "pay_date": _text_date(payment.pay_date),
        "amount": float(payment.amount),
        "vat": float(payment.vat),
        "currency": payment.currency,
        "supplier_id": int(payment.supplier_id),
        "recipient": payment.recipient.strip(),
        "recipient_key": recipient_key(payment.recipient),
        "status": payment.status.value,
        "source_status": payment.source_status,
        "paid_flag": int(payment.paid_flag),
        "operation": payment.operation,
        "over_limit": int(payment.over_limit),
        "priority": payment.priority,
        "edo_state": payment.edo_state,
        "responsible": payment.responsible,
        "author": payment.author,
        "comment": payment.comment,
        "had_files": int(payment.had_files),
        "origin": payment.origin.value,
        "origin_ref": payment.origin_ref,
    }
    if not imported_only:
        return everything
    return {name: everything[name] for name in IMPORTED_FIELDS}


def _payment(row: sqlite3.Row) -> Payment:
    keys = row.keys()
    return Payment(
        id=int(row["id"]),
        doc_number=row["doc_number"],
        request_date=_date(row["request_date"]),
        pay_date=_date(row["pay_date"]),
        amount=float(row["amount"]),
        vat=float(row["vat"]),
        currency=row["currency"],
        supplier_id=int(row["supplier_id"]),
        recipient=row["recipient"],
        status=_status(row["status"]),
        source_status=row["source_status"],
        paid_flag=bool(row["paid_flag"]),
        operation=row["operation"],
        over_limit=bool(row["over_limit"]),
        priority=row["priority"],
        edo_state=row["edo_state"],
        responsible=row["responsible"],
        author=row["author"],
        comment=row["comment"],
        had_files=bool(row["had_files"]),
        origin=_origin(row["origin"]),
        origin_ref=row["origin_ref"],
        created_at=_moment(row["created_at"]),
        updated_at=_moment(row["updated_at"]),
        files=int(row["files"]) if "files" in keys else 0,
    )


def _budget(row: sqlite3.Row) -> Budget:
    return Budget(
        year=int(row["year"]),
        month=int(row["month"]),
        amount=float(row["amount"]),
        note=row["note"],
        updated_at=_moment(row["updated_at"]),
    )


def _file(row: sqlite3.Row) -> PaymentFile:
    return PaymentFile(
        id=int(row["id"]),
        payment_id=int(row["payment_id"]),
        name=row["name"],
        path=row["path"],
        size=int(row["size"]),
        added_at=_moment(row["added_at"]),
    )


def _status(value: Any) -> PaymentStatus:
    try:
        return PaymentStatus(str(value))
    except ValueError:
        return PaymentStatus.PLANNED


def _origin(value: Any) -> PaymentOrigin:
    try:
        return PaymentOrigin(str(value))
    except ValueError:
        return PaymentOrigin.MANUAL


def _date(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text))
    except (ValueError, TypeError):
        return None


def _text_date(value: date | None) -> str:
    return value.isoformat() if value else ""


def _moment(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text))
    except (ValueError, TypeError):
        return None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _drop_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — имя пользователя не критично
        return ""
