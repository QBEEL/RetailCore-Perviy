"""Выгрузка локальной базы администратора в общую.

Отдельно от импорта, хотя механика похожа. Разница в правиле: импорт 1С
обновляет только поля выгрузки и бережёт то, что человек правил руками, а здесь
локальная запись побеждает целиком — выгрузку делает тот, кто эти данные и
готовил, и наполовину применённая правка была бы хуже отсутствия правки.

Сервер, как и при импорте, не решает, что кому соответствует: пары «моя запись —
серверная» составляет приложение, у которого перед глазами обе базы. Сюда
приходит уже готовый список.

Записи, которых в выгрузке нет, не трогаются. Выгрузка сливает, а не зеркалит:
пока администратор работал у себя, отдел работал в общей базе.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import db, security
from ..schemas import Origin, Status
from ..security import User

router = APIRouter(prefix="/api/sync", tags=["Выгрузка"])

# Все поля оплаты, кроме служебных. `author` сохраняется тот, что в локальной
# базе: запись создавал он, а не тот, кто её сегодня выгружает.
FIELDS: tuple[str, ...] = (
    "doc_number", "request_date", "pay_date", "amount", "vat", "currency",
    "supplier_id", "recipient", "recipient_key", "status", "source_status",
    "paid_flag", "operation", "over_limit", "priority", "edo_state",
    "responsible", "author", "comment", "had_files", "origin", "origin_ref",
)


class SyncPayment(BaseModel):
    """Одна оплата из локальной базы."""

    doc_number: str = ""
    request_date: date | None = None
    pay_date: date | None = None
    amount: float = 0.0
    vat: float = 0.0
    currency: str = "руб."
    supplier_id: int = 0
    recipient: str = ""
    recipient_key: str = ""
    status: Status = "planned"
    source_status: str = ""
    paid_flag: bool = False
    operation: str = ""
    over_limit: bool = False
    priority: str = ""
    edo_state: str = ""
    responsible: str = ""
    author: str = ""
    comment: str = ""
    had_files: bool = False
    origin: Origin = "manual"
    origin_ref: str = ""


class SyncChange(BaseModel):
    """Серверная запись и то, чем её заменить."""

    id: int
    payment: SyncPayment


class SyncBudget(BaseModel):
    year: int
    month: int = Field(ge=1, le=12)
    amount: float = 0.0
    note: str = ""


class SyncUpload(BaseModel):
    # Предел с запасом на полную историю: около семи тысяч строк. Больше —
    # почти наверняка ошибка, а не выгрузка, и бесконечная транзакция ни к чему.
    created: list[SyncPayment] = Field(default=[], max_length=20000)
    changed: list[SyncChange] = Field(default=[], max_length=20000)
    budgets: list[SyncBudget] = Field(default=[], max_length=600)


class SyncResult(BaseModel):
    new: int
    updated: int
    budgets: int


@router.post("/upload", response_model=SyncResult,
             summary="Выгрузить локальную базу в общую")
def upload(form: SyncUpload,
           user: User = Depends(security.admin_only)) -> SyncResult:
    """Одной транзакцией: либо применяется вся выгрузка, либо ничего."""
    with db.cursor() as handle:
        if form.created:
            columns = ", ".join(FIELDS)
            marks = ", ".join(["%s"] * len(FIELDS))
            handle.executemany(
                f"INSERT INTO payment ({columns}, updated_by)"
                f" VALUES ({marks}, %s)"
                # Строку из 1С могли залить и напрямую импортом, пока шла
                # подготовка: повтор не должен рвать всю транзакцию.
                " ON CONFLICT (doc_number, request_date)"
                "   WHERE doc_number <> '' DO NOTHING",
                [[*_row(item, FIELDS), user.id] for item in form.created])

        if form.changed:
            assignments = ", ".join(f"{name} = %s" for name in FIELDS)
            handle.executemany(
                f"UPDATE payment SET {assignments}, updated_at = now(),"
                " updated_by = %s WHERE id = %s",
                [[*_row(item.payment, FIELDS), user.id, item.id]
                 for item in form.changed])

        if form.budgets:
            handle.executemany(
                "INSERT INTO budget (year, month, amount, note, updated_at,"
                "  updated_by) VALUES (%s, %s, %s, %s, now(), %s)"
                " ON CONFLICT (year, month) DO UPDATE"
                "   SET amount = EXCLUDED.amount, note = EXCLUDED.note,"
                "       updated_at = now(), updated_by = EXCLUDED.updated_by",
                [[item.year, item.month, item.amount, item.note, user.id]
                 for item in form.budgets])

        # Одна запись на выгрузку, а не на строку: тысячи одинаковых строк
        # заслонили бы в журнале ручные правки, ради которых он и ведётся.
        handle.execute(
            "INSERT INTO audit_log (user_id, entity, action, changes)"
            " VALUES (%s, 'payment', 'upload', %s)",
            (user.id, json.dumps({"новых": len(form.created),
                                  "обновлено": len(form.changed),
                                  "бюджетов": len(form.budgets)})))

    return SyncResult(new=len(form.created), updated=len(form.changed),
                      budgets=len(form.budgets))


def _row(item: SyncPayment, fields: tuple[str, ...]) -> list[Any]:
    return [getattr(item, name) for name in fields]
