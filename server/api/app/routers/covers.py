"""Замещение на время отпуска: кто правит чьи оплаты, пока человека нет.

Назначает администратор — так же, как и закрепление имён из 1С за учёткой:
право править чужие оплаты не должно выдаваться самими менеджерами. Сама
проверка при правке живёт в `security.User.may_edit`; здесь только записи
о замещении.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from .. import auditlog, db, security
from ..schemas import CoverIn, CoverOut
from ..security import User

router = APIRouter(prefix="/api/covers", tags=["Замещение"])

# Сегодняшняя дата по Москве — той же, по которой security.current_user решает,
# идёт ли замещение. Считать в другом часовом поясе значило бы разойтись с ним
# на несколько часов вокруг полуночи.
_TODAY = "(now() AT TIME ZONE 'Europe/Moscow')::date"

_SELECT = (
    "SELECT c.id, c.absent_id, a.full_name AS absent_name,"
    "       c.cover_id, v.full_name AS cover_name,"
    "       c.date_from, c.date_to, c.note, c.created_at,"
    "       COALESCE(u.full_name, '') AS created_by,"
    f"      CASE WHEN {_TODAY} < c.date_from THEN 'planned'"
    f"           WHEN {_TODAY} > c.date_to THEN 'ended'"
    "            ELSE 'active' END AS state"
    " FROM user_cover c"
    " JOIN app_user a ON a.id = c.absent_id"
    " JOIN app_user v ON v.id = c.cover_id"
    " LEFT JOIN app_user u ON u.id = c.created_by"
)


def _load(cover_id: int) -> dict:
    row = db.fetch_one(_SELECT + " WHERE c.id = %s", (cover_id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Замещение не найдено")
    return row


def _check(form: CoverIn) -> tuple[dict, dict]:
    if form.absent_id == form.cover_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Человек не может замещать сам себя")
    if form.date_to < form.date_from:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Конец замещения раньше начала")
    people = {}
    for person_id in (form.absent_id, form.cover_id):
        row = db.fetch_one(
            "SELECT id, full_name, login, is_active FROM app_user WHERE id = %s",
            (person_id,))
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Учётная запись не найдена")
        people[person_id] = row
    cover = people[form.cover_id]
    if not cover["is_active"]:
        # Отключённая учётка войти не может: замещение осталось бы пустым
        # обещанием, а оплаты — без присмотра.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Учётная запись {cover['full_name'] or cover['login']} "
                   "отключена — заместить она не сможет")
    return people[form.absent_id], cover


def _snapshot(form: CoverIn, absent: dict, cover: dict) -> dict:
    """Что записать в журнал. Имена, а не номера: через год по «17 → 23» не понять."""
    absent_name = absent["full_name"] or absent["login"]
    cover_name = cover["full_name"] or cover["login"]
    return {
        "absent": absent_name, "cover": cover_name,
        "date_from": form.date_from, "date_to": form.date_to, "note": form.note,
        auditlog.ABOUT: {
            "title": f"{cover_name} за {absent_name}, "
                     f"{form.date_from:%d.%m.%Y}–{form.date_to:%d.%m.%Y}"},
    }


@router.get("", response_model=list[CoverOut], summary="Замещения")
def list_covers(user: User = Depends(security.admin_only)) -> list[CoverOut]:
    # Идущие и будущие — сверху, закончившиеся — следом: администратору нужно
    # прежде всего то, что действует.
    rows = db.fetch_all(
        _SELECT + " ORDER BY (c.date_to < " + _TODAY + "), c.date_from DESC, c.id DESC"
        " LIMIT 500")
    return [CoverOut(**row) for row in rows]


@router.post("", response_model=CoverOut, status_code=status.HTTP_201_CREATED,
             summary="Назначить замещение")
def create_cover(form: CoverIn,
                 user: User = Depends(security.admin_only)) -> CoverOut:
    absent, cover = _check(form)
    created = db.fetch_one(
        "INSERT INTO user_cover (absent_id, cover_id, date_from, date_to, note,"
        "                        created_by)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (form.absent_id, form.cover_id, form.date_from, form.date_to,
         form.note.strip(), user.id))
    auditlog.record(user.id, "cover", created["id"], "create",
                    _snapshot(form, absent, cover))
    return CoverOut(**_load(created["id"]))


@router.put("/{cover_id}", response_model=CoverOut, summary="Изменить замещение")
def update_cover(cover_id: int, form: CoverIn,
                 user: User = Depends(security.admin_only)) -> CoverOut:
    _load(cover_id)
    absent, cover = _check(form)
    db.execute(
        "UPDATE user_cover SET absent_id = %s, cover_id = %s, date_from = %s,"
        "                      date_to = %s, note = %s WHERE id = %s",
        (form.absent_id, form.cover_id, form.date_from, form.date_to,
         form.note.strip(), cover_id))
    auditlog.record(user.id, "cover", cover_id, "update",
                    _snapshot(form, absent, cover))
    return CoverOut(**_load(cover_id))


@router.delete("/{cover_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Снять замещение")
def delete_cover(cover_id: int, user: User = Depends(security.admin_only)) -> None:
    row = _load(cover_id)
    db.execute("DELETE FROM user_cover WHERE id = %s", (cover_id,))
    # Право пропадает сразу: security.current_user читает замещения на каждом
    # запросе, токен перевыпускать не нужно.
    auditlog.record(user.id, "cover", cover_id, "delete", {
        "absent": row["absent_name"], "cover": row["cover_name"],
        "date_from": row["date_from"], "date_to": row["date_to"],
        auditlog.ABOUT: {
            "title": f"{row['cover_name']} за {row['absent_name']}, "
                     f"{row['date_from']:%d.%m.%Y}–{row['date_to']:%d.%m.%Y}"},
    })
