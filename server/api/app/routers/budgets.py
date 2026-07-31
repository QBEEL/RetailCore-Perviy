"""Бюджеты по месяцам.

Бюджет общий для отдела, а не личный, поэтому правит его только администратор:
иначе двенадцать человек переписывали бы одну и ту же цифру по очереди.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, status

from .. import db, security
from ..schemas import BudgetIn, BudgetOut
from ..security import User

router = APIRouter(prefix="/api/budgets", tags=["Бюджеты"])


@router.get("", response_model=list[BudgetOut], summary="Все бюджеты")
def list_budgets(user: User = Depends(security.current_user)) -> list[BudgetOut]:
    rows = db.fetch_all(
        "SELECT year, month, amount, note, updated_at FROM budget"
        " ORDER BY year DESC, month DESC")
    return [BudgetOut(**{**r, "amount": float(r["amount"])}) for r in rows]


@router.put("", response_model=BudgetOut, summary="Задать бюджет месяца")
def save_budget(form: BudgetIn,
                user: User = Depends(security.admin_only)) -> BudgetOut:
    row = db.fetch_one(
        "INSERT INTO budget (year, month, amount, note, updated_by)"
        " VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (year, month) DO UPDATE"
        " SET amount = EXCLUDED.amount, note = EXCLUDED.note,"
        "     updated_at = now(), updated_by = EXCLUDED.updated_by"
        " RETURNING year, month, amount, note, updated_at",
        (form.year, form.month, form.amount, form.note, user.id))
    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action, changes)"
        " VALUES (%s, 'budget', %s, 'save', %s)",
        (user.id, form.year * 100 + form.month,
         json.dumps({"amount": form.amount, "note": form.note})))
    return BudgetOut(**{**row, "amount": float(row["amount"])})


@router.delete("/{year}/{month}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Убрать бюджет месяца")
def delete_budget(year: int, month: int,
                  user: User = Depends(security.admin_only)) -> None:
    db.execute("DELETE FROM budget WHERE year = %s AND month = %s",
               (year, month))
    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action)"
        " VALUES (%s, 'budget', %s, 'delete')",
        (user.id, year * 100 + month))
