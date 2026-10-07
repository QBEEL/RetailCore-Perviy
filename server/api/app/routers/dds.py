"""Справочник статей ДДС оплат.

Читать его может любой вошедший — из него менеджер выбирает статью в карточке
оплаты. Добавляет статьи только администратор: справочник общий, и опечатка
или дубль, внесённые одним человеком, мешали бы всем.

Цвет и метку задаёт администратор (`PATCH`): оплаты статьи в таблице и
календаре получают цветной кружок и подпись. Они хранятся в справочнике, а не в
оплатах, поэтому смена цвета действует на все оплаты статьи сразу.

Удаления и переименования здесь нет намеренно: название статьи записано прямо в
оплатах, и переименование оставило бы в них название, которого в справочнике
уже нет.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status

from .. import db, security
from ..schemas import DdsItemIn, DdsItemOut, DdsMarkIn
from ..security import User

router = APIRouter(prefix="/api/dds-items", tags=["Статьи ДДС"])


@router.get("", response_model=list[DdsItemOut], summary="Справочник статей ДДС")
def items(user: User = Depends(security.current_user)) -> list[DdsItemOut]:
    # По алфавиту без учёта регистра: добавленная администратором статья
    # встаёт на своё место, а не прячется в конце списка.
    return [DdsItemOut(**row) for row in db.fetch_all(
        "SELECT id, title, color, note FROM dds_item ORDER BY lower(title)")]


@router.post("", response_model=DdsItemOut, status_code=status.HTTP_201_CREATED,
             summary="Добавить статью ДДС")
def add(form: DdsItemIn, user: User = Depends(security.admin_only)) -> DdsItemOut:
    # Пробелы по краям и двойные внутри убираются: такие названия выглядят как
    # одна статья, а для сравнения были бы двумя.
    title = " ".join(form.title.split())
    if not title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Название статьи не может быть пустым")
    if title.casefold() == "none":
        # Это слово клиент шлёт в отборе «оплаты без статьи».
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Такое название зарезервировано")
    created = db.fetch_one(
        "INSERT INTO dds_item (title, created_by) VALUES (%s, %s)"
        " ON CONFLICT (lower(title)) DO NOTHING RETURNING id, title, color, note",
        (title, user.id))
    if not created:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Статья «{title}» уже есть в справочнике")
    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action, changes)"
        " VALUES (%s, 'dds_item', %s, 'create', %s)",
        (user.id, created["id"], json.dumps({"title": title})))
    return DdsItemOut(**created)


@router.patch("", response_model=list[DdsItemOut], summary="Цвет и метка статей")
def mark(form: DdsMarkIn, user: User = Depends(security.admin_only)) -> list[DdsItemOut]:
    """Один цвет и одна метка для всех выбранных статей.

    Пустые значения снимают цвет и метку. Метка очищается от лишних пробелов,
    цвет приводится к верхнему регистру — так «#ec4899» и «#EC4899» не дают двух
    разных цветов в сравнении.
    """
    note = " ".join(form.note.split())
    color = form.color.upper()
    with db.cursor() as handle:
        handle.execute(
            "UPDATE dds_item SET color = %s, note = %s WHERE id = ANY(%s)"
            " RETURNING id, title, color, note",
            (color, note, form.ids))
        rows = handle.fetchall()
        if not rows:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Статьи не найдены")
        handle.execute(
            "INSERT INTO audit_log (user_id, entity, entity_id, action, changes)"
            " VALUES (%s, 'dds_item', 0, 'update_many', %s)",
            (user.id, json.dumps({"ids": len(rows), "color": color, "note": note})))
    return [DdsItemOut(**row) for row in rows]
