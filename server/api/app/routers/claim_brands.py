"""Справочник брендов для заявок Торг-2.

Нужен ради подсказки при наборе: склад вводит бренд руками, и без списка один и
тот же писали пятью способами. Заявка при этом хранит бренд текстом, а не
ссылкой — см. `db/migrations/014_claim_brands.sql`.

Добавлять может любой: нового бренда на приёмке никто ждать не будет, а в
таблицу склада его вписывали так же свободно. Удалять — только администратор:
опечатку убрать нужно, но стирать бренд, на который уже опираются подсказки
всего склада, по ошибке случайного человека нельзя.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .. import auditlog, db, security
from ..security import User

router = APIRouter(prefix="/api/claim-brands", tags=["Торг-2"])


class BrandIn(BaseModel):
    name: str = Field(..., max_length=200)


class BrandOut(BaseModel):
    id: int
    name: str


def _clean(name: str) -> str:
    return " ".join(name.split())


def _key(name: str) -> str:
    """Ключ сравнения — тот же, что считает миграция при начальной загрузке."""
    return name.lower().replace("ё", "е")


@router.get("", response_model=list[BrandOut], summary="Все бренды")
def list_brands(user: User = Depends(security.current_user)) -> list[BrandOut]:
    return [BrandOut(**row) for row in db.fetch_all(
        "SELECT id, name FROM claim_brand ORDER BY name_key")]


@router.post("", response_model=BrandOut, summary="Добавить бренд")
def add_brand(form: BrandIn,
              user: User = Depends(security.current_user)) -> BrandOut:
    """Добавляет бренд. Если такой уже есть — отдаёт существующий, а не ошибку:
    двое могли добавить его одновременно, и для второго это не сбой."""
    name = _clean(form.name)
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Название бренда пустое")
    row = db.fetch_one(
        "INSERT INTO claim_brand (name, name_key, added_by) VALUES (%s, %s, %s)"
        " ON CONFLICT (name_key) DO NOTHING RETURNING id, name",
        (name, _key(name), user.id))
    if row:
        auditlog.record(user.id, "claim_brand", row["id"], "create",
                        {"name": name, auditlog.ABOUT: {"title": name}})
        return BrandOut(**row)
    return BrandOut(**db.fetch_one(
        "SELECT id, name FROM claim_brand WHERE name_key = %s", (_key(name),)))


@router.delete("/{brand_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Убрать бренд")
def delete_brand(brand_id: int,
                 user: User = Depends(security.admin_only)) -> None:
    row = db.fetch_one("SELECT name FROM claim_brand WHERE id = %s", (brand_id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Бренда нет в списке")
    db.execute("DELETE FROM claim_brand WHERE id = %s", (brand_id,))
    auditlog.record(user.id, "claim_brand", brand_id, "delete",
                    {auditlog.ABOUT: {"title": row["name"]}})
