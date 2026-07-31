"""Оплаты: отбор, чтение, создание, правка, удаление.

Отбор повторяет `Filter` из приложения — те же поля и та же логика «пустое
условие не ограничивает выборку». Условия собираются только через параметры
запроса: подставлять значения в текст SQL нельзя ни при каких обстоятельствах.

Выборка отдаётся целиком, без страниц. Вся история — около семи тысяч строк;
постранично здесь пришлось бы усложнять сортировку и массовые операции, ничего
не выигрывая.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import db, security
from ..schemas import (
    BulkPatch,
    BulkResult,
    KnownValues,
    PaymentIn,
    PaymentOut,
    PaymentPatch,
)
from ..security import User

router = APIRouter(prefix="/api/payments", tags=["Оплаты"])

# Столбцы платежа в порядке, ожидаемом PaymentOut.
COLUMNS = (
    "p.id, p.doc_number, p.request_date, p.pay_date, p.amount, p.vat,"
    " p.currency, p.supplier_id, p.recipient, p.recipient_key, p.status,"
    " p.source_status, p.paid_flag, p.operation, p.over_limit, p.priority,"
    " p.edo_state, p.responsible, p.author, p.comment, p.had_files,"
    " p.origin, p.origin_ref, p.created_at, p.updated_at,"
    " (SELECT COUNT(*) FROM payment_file f WHERE f.payment_id = p.id) AS files"
)

# Правки, разрешённые обычному пользователю. Поля из выгрузки 1С сюда не входят:
# их перезапишет следующий импорт, и правка всё равно пропадёт.
EDITABLE = ("pay_date", "status", "comment", "supplier_id", "amount", "priority")


def _conditions(
    text: str, start: date | None, end: date | None,
    statuses: list[str], supplier_id: int, recipient: str,
    amount_from: float | None, amount_to: float | None,
    responsible: str, operation: str, over_limit: bool | None,
    dated_only: bool,
) -> tuple[str, list[Any]]:
    parts: list[str] = []
    values: list[Any] = []
    if text:
        like = f"%{text.strip()}%"
        parts.append("(p.recipient ILIKE %s OR p.doc_number ILIKE %s"
                     " OR p.comment ILIKE %s OR p.responsible ILIKE %s"
                     " OR p.author ILIKE %s)")
        values.extend([like] * 5)
    if start:
        parts.append("p.pay_date >= %s")
        values.append(start)
    if end:
        parts.append("p.pay_date <= %s")
        values.append(end)
    if dated_only:
        parts.append("p.pay_date IS NOT NULL")
    if statuses:
        parts.append("p.status = ANY(%s)")
        values.append(statuses)
    if supplier_id:
        parts.append("p.supplier_id = %s")
        values.append(supplier_id)
    if recipient:
        parts.append("p.recipient_key = %s")
        values.append(recipient)
    if amount_from is not None:
        parts.append("p.amount >= %s")
        values.append(amount_from)
    if amount_to is not None:
        parts.append("p.amount <= %s")
        values.append(amount_to)
    if responsible:
        parts.append("p.responsible = %s")
        values.append(responsible)
    if operation:
        parts.append("p.operation = %s")
        values.append(operation)
    if over_limit is not None:
        parts.append("p.over_limit = %s")
        values.append(over_limit)
    return (" AND ".join(parts) if parts else "TRUE"), values


def _out(row: dict, user: User) -> PaymentOut:
    row = dict(row)
    row["amount"] = float(row["amount"])
    row["vat"] = float(row["vat"])
    row["editable"] = user.may_edit(row["responsible"])
    return PaymentOut(**row)


@router.get("", response_model=list[PaymentOut], summary="Отбор оплат")
def list_payments(
    user: User = Depends(security.current_user),
    text: str = "",
    start: date | None = None,
    end: date | None = None,
    statuses: list[str] = Query(default=[]),
    supplier_id: int = 0,
    recipient: str = "",
    amount_from: float | None = None,
    amount_to: float | None = None,
    responsible: str = "",
    operation: str = "",
    over_limit: bool | None = None,
    dated_only: bool = False,
    mine: bool = False,
) -> list[PaymentOut]:
    where, values = _conditions(
        text, start, end, statuses, supplier_id, recipient,
        amount_from, amount_to, responsible, operation, over_limit, dated_only)
    if mine and not user.is_admin:
        where += " AND p.responsible = ANY(%s)"
        values.append(list(user.responsible))
    rows = db.fetch_all(
        f"SELECT {COLUMNS} FROM payment p WHERE {where}"
        " ORDER BY p.pay_date DESC NULLS LAST, p.id DESC", values)
    return [_out(row, user) for row in rows]


@router.get("/known", response_model=KnownValues,
            summary="Значения для выпадающих списков")
def known_values(user: User = Depends(security.current_user)) -> KnownValues:
    def by_frequency(column: str, group: str) -> list[str]:
        # Частые значения сверху — так же, как считала локальная база: в списке
        # из сотен получателей нужный обычно среди первых. Имена столбцов не
        # приходят извне, они литералы этого файла.
        rows = db.fetch_all(
            f"SELECT {column} AS value, COUNT(*) AS n FROM payment"
            f" WHERE {column} <> '' GROUP BY {group}"
            f" ORDER BY COUNT(*) DESC, {column}")
        return [r["value"] for r in rows]

    return KnownValues(
        recipients=by_frequency("recipient", "recipient, recipient_key"),
        responsible=by_frequency("responsible", "responsible"),
        operations=by_frequency("operation", "operation"),
    )


@router.get("/count", response_model=dict, summary="Сколько всего оплат")
def count_payments(user: User = Depends(security.current_user)) -> dict:
    row = db.fetch_one("SELECT COUNT(*) AS total FROM payment")
    return {"total": row["total"]}


@router.post("/refresh-overdue", response_model=dict,
             summary="Перевести запланированное с ушедшей датой в просрочку")
def refresh_overdue(user: User = Depends(security.current_user)) -> dict:
    """Пересчёт производного состояния, а не правка чужих записей.

    Затрагивается только «Запланировано». Оплаченное и отменённое не
    пересчитывается никогда: отклонённые заявки прошлых лет иначе стали бы
    просрочкой и повисли вечным долгом. «Перенесено» тоже не трогаем — у
    переноса есть новая дата, назначенная человеком.

    Поэтому и разрешено всем, а не только владельцу оплаты: результат зависит
    от календаря, а не от того, кто нажал.
    """
    changed = db.execute(
        "UPDATE payment SET status = 'overdue', updated_at = now()"
        " WHERE status = 'planned' AND pay_date IS NOT NULL"
        "   AND pay_date < CURRENT_DATE")
    return {"changed": changed}


@router.get("/{payment_id}", response_model=PaymentOut, summary="Одна оплата")
def get_payment(payment_id: int,
                user: User = Depends(security.current_user)) -> PaymentOut:
    row = db.fetch_one(f"SELECT {COLUMNS} FROM payment p WHERE p.id = %s",
                       (payment_id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Оплата не найдена")
    return _out(row, user)


@router.post("", response_model=PaymentOut,
             status_code=status.HTTP_201_CREATED, summary="Создать оплату")
def create_payment(form: PaymentIn,
                   user: User = Depends(security.current_user)) -> PaymentOut:
    # Ответственный по умолчанию — сам автор. Назначать оплату на другого
    # человека может только администратор, иначе правило «правлю своё»
    # обходилось бы созданием записи на чужое имя.
    responsible = form.responsible or user.full_name
    if not user.may_edit(responsible):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Создать оплату на другого ответственного нельзя")

    created = db.fetch_one(
        "INSERT INTO payment (pay_date, amount, vat, currency, supplier_id,"
        "  recipient, recipient_key, status, operation, priority, comment,"
        "  responsible, author, origin, updated_by)"
        " VALUES (%s, %s, %s, %s, %s, %s, lower(btrim(%s)), %s, %s, %s, %s,"
        "         %s, %s, 'manual', %s) RETURNING id",
        (form.pay_date, form.amount, form.vat, form.currency, form.supplier_id,
         form.recipient, form.recipient, form.status, form.operation,
         form.priority, form.comment, responsible, user.full_name, user.id))
    _record(user, "payment", created["id"], "create",
            {"recipient": form.recipient})
    return get_payment(created["id"], user)


def _changes(form: PaymentPatch) -> dict[str, Any]:
    """Переданные поля правки. Не переданные в словарь не попадают."""
    given = form.model_dump(exclude_unset=True)
    values = {name: given[name] for name in EDITABLE if name in given}
    if form.clear_pay_date:
        values["pay_date"] = None
    # Отметка об оплате должна следовать за статусом, иначе отчёты разойдутся:
    # в приложении это же правило соблюдает store.update_many.
    if "status" in values:
        values["paid_flag"] = values["status"] == "paid"
    return values


def _record(user: User, entity: str, entity_id: int, action: str,
            changes: dict[str, Any]) -> None:
    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action, changes)"
        " VALUES (%s, %s, %s, %s, %s)",
        (user.id, entity, entity_id, action, json.dumps(changes, default=str)))


@router.patch("/{payment_id}", response_model=PaymentOut,
              summary="Изменить оплату")
def patch_payment(payment_id: int, form: PaymentPatch,
                  user: User = Depends(security.current_user)) -> PaymentOut:
    owner = db.fetch_one("SELECT responsible FROM payment WHERE id = %s",
                         (payment_id,))
    if not owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Оплата не найдена")
    if not user.may_edit(owner["responsible"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Оплата закреплена за другим менеджером:"
                   f" {owner['responsible']}")

    values = _changes(form)
    if not values:
        return get_payment(payment_id, user)

    # Имена столбцов берутся из EDITABLE и paid_flag — литералов этого файла,
    # а не из запроса, поэтому подстановка их в текст SQL безопасна. Значения,
    # как и везде, уходят параметрами.
    assignments = ", ".join(f"{name} = %s" for name in values)
    db.execute(
        f"UPDATE payment SET {assignments}, updated_at = now(), updated_by = %s"
        " WHERE id = %s", [*values.values(), user.id, payment_id])
    _record(user, "payment", payment_id, "update", values)
    return get_payment(payment_id, user)


@router.patch("", response_model=BulkResult, summary="Изменить несколько оплат")
def patch_many(form: BulkPatch,
               user: User = Depends(security.current_user)) -> BulkResult:
    values = _changes(form.patch)
    if not values:
        return BulkResult(changed=0)

    rows = db.fetch_all(
        "SELECT id, responsible FROM payment WHERE id = ANY(%s)", (form.ids,))
    allowed = [r["id"] for r in rows if user.may_edit(r["responsible"])]
    denied = [r["id"] for r in rows if not user.may_edit(r["responsible"])]
    if not allowed:
        return BulkResult(changed=0, denied=denied)

    assignments = ", ".join(f"{name} = %s" for name in values)
    changed = db.execute(
        f"UPDATE payment SET {assignments}, updated_at = now(), updated_by = %s"
        " WHERE id = ANY(%s)", [*values.values(), user.id, allowed])
    _record(user, "payment", 0, "update_many",
            {"ids": len(allowed), **{k: str(v) for k, v in values.items()}})
    return BulkResult(changed=changed, denied=denied)


@router.delete("/{payment_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Удалить оплату")
def delete_payment(payment_id: int,
                   user: User = Depends(security.current_user)) -> None:
    row = db.fetch_one("SELECT responsible, origin FROM payment WHERE id = %s",
                       (payment_id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Оплата не найдена")
    if not user.may_edit(row["responsible"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Оплата закреплена за другим менеджером")
    if row["origin"] == "import" and not user.is_admin:
        # Запись из 1С вернётся при следующем импорте, а её комментарии и
        # вложения — нет. Удалять такое вправе только администратор.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Оплата пришла из 1С — удалить может только администратор")
    db.execute("DELETE FROM payment WHERE id = %s", (payment_id,))
    _record(user, "payment", payment_id, "delete", {})
