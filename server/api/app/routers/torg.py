"""Акты ТОРГ-2: хранение, нумерация, реквизиты.

Бланк Excel собирает приложение; здесь лежит то, из чего он собран. Так акт
можно открыть и поправить, номер не набирается заново, а поставщик и договор
подсказываются из прошлых актов.

Права простые, как у заявок: составлять и править акт может любой вошедший —
склад работает сменами, и «чужого» акта у склада нет. Каждое действие попадает
в журнал.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import errors
from pydantic import BaseModel, Field

from .. import auditlog, db, security
from ..security import User

router = APIRouter(prefix="/api/torg", tags=["Торг-2"])

SETTINGS_KEY = "torg"


class TorgSettings(BaseModel):
    organization: str = Field("", max_length=500)
    department: str = Field("", max_length=200)
    director_title: str = Field("", max_length=200)
    director_name: str = Field("", max_length=200)
    place: str = Field("", max_length=500)
    chair_title: str = Field("", max_length=200)
    chair_name: str = Field("", max_length=200)
    # Должность и «Фамилия И.». В бланке под членов комиссии три строки.
    members: list[list[str]] = Field(default_factory=list, max_length=3)


class ActIn(BaseModel):
    number: int = Field(..., gt=0, lt=1_000_000)
    act_date: date
    supplier: str = Field("", max_length=600)
    payload: dict[str, Any] = Field(default_factory=dict)
    claim_ids: list[int] = Field(default_factory=list, max_length=200)


class ActOut(BaseModel):
    id: int
    number: int
    act_date: date
    supplier: str
    payload: dict[str, Any]
    claim_ids: list[int]
    updated_at: datetime


class SupplierHint(BaseModel):
    supplier: str
    contract_number: str = ""
    contract_date: str = ""
    delivery: str = ""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _title(number: int, supplier: str) -> dict[str, Any]:
    """Как акт назвать в журнале: удалённый акт иначе не узнать."""
    head = supplier.split(",")[0].strip()[:60]
    return {"title": f"ТОРГ-2 №{number} {head}".strip()}


def _out(row: dict) -> ActOut:
    ids = db.fetch_all("SELECT id FROM claim WHERE act_id = %s ORDER BY id", (row["id"],))
    return ActOut(id=row["id"], number=row["number"], act_date=row["act_date"],
                  supplier=row["supplier"], payload=row["payload"] or {},
                  claim_ids=[r["id"] for r in ids], updated_at=row["updated_at"])


def _load(act_id: int) -> dict:
    row = db.fetch_one(
        "SELECT id, number, act_date, supplier, payload, updated_at"
        " FROM torg_act WHERE id = %s", (act_id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Акт не найден")
    return row


def _check_claims(claim_ids: list[int], act_id: int) -> None:
    """Все заявки существуют и не лежат в чужом акте."""
    if not claim_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="В акте нет ни одной заявки")
    rows = db.fetch_all(
        "SELECT c.id, c.act_id, t.number AS act_number FROM claim c"
        " LEFT JOIN torg_act t ON t.id = c.act_id WHERE c.id = ANY(%s)", (claim_ids,))
    if len(rows) != len(set(claim_ids)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Одной из заявок уже нет — обновите список")
    for row in rows:
        if row["act_id"] not in (None, act_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Заявка №{row['id']} уже включена в акт №{row['act_number']}")


def _link(act_id: int, claim_ids: list[int]) -> None:
    # Убранные из акта заявки освобождаются, включённые — привязываются.
    db.execute("UPDATE claim SET act_id = NULL WHERE act_id = %s AND NOT (id = ANY(%s))",
               (act_id, claim_ids))
    db.execute("UPDATE claim SET act_id = %s WHERE id = ANY(%s)", (act_id, claim_ids))


def _duplicate(number: int, year: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Акт №{number} за {year} год уже составлен. Номер ведётся "
               "по порядку — возьмите следующий")


# --- реквизиты ----------------------------------------------------------------

@router.get("/settings", response_model=TorgSettings, summary="Реквизиты для бланка")
def get_settings(user: User = Depends(security.current_user)) -> TorgSettings:
    row = db.fetch_one("SELECT value FROM claim_setting WHERE key = %s", (SETTINGS_KEY,))
    return TorgSettings(**(row["value"] if row else {}))


@router.put("/settings", response_model=TorgSettings, summary="Сохранить реквизиты")
def put_settings(form: TorgSettings,
                 user: User = Depends(security.current_user)) -> TorgSettings:
    body = form.model_dump()
    body["members"] = [m[:2] for m in body["members"] if len(m) >= 2][:3]
    old = db.fetch_one("SELECT value FROM claim_setting WHERE key = %s", (SETTINGS_KEY,))
    db.execute(
        "INSERT INTO claim_setting (key, value, updated_by) VALUES (%s, %s::jsonb, %s)"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,"
        "   updated_by = EXCLUDED.updated_by, updated_at = now()",
        (SETTINGS_KEY, _json(body), user.id))
    changes = auditlog.diff((old or {}).get("value") or {}, body)
    auditlog.record(user.id, "torg_setting", 0, "update",
                    {**changes, auditlog.ABOUT: {"title": "Реквизиты ТОРГ-2"}})
    return TorgSettings(**body)


# --- справочное ---------------------------------------------------------------

@router.get("/next", response_model=dict, summary="Следующий номер акта")
def next_number(year: int, user: User = Depends(security.current_user)) -> dict:
    row = db.fetch_one("SELECT COALESCE(MAX(number), 0) + 1 AS number"
                       " FROM torg_act WHERE act_year = %s", (year,))
    return {"number": int(row["number"])}


@router.get("/suppliers", response_model=list[SupplierHint],
            summary="Поставщики из прошлых актов")
def suppliers(user: User = Depends(security.current_user)) -> list[SupplierHint]:
    """Каждый поставщик — с договором и способом доставки из его последнего акта:
    они почти всегда те же, и набирать их заново незачем."""
    rows = db.fetch_all(
        "SELECT DISTINCT ON (supplier) supplier,"
        "       payload->>'contract_number' AS contract_number,"
        "       payload->>'contract_date' AS contract_date,"
        "       payload->>'delivery' AS delivery"
        " FROM torg_act WHERE supplier <> ''"
        " ORDER BY supplier, id DESC")
    return [SupplierHint(supplier=r["supplier"],
                         contract_number=r["contract_number"] or "",
                         contract_date=r["contract_date"] or "",
                         delivery=r["delivery"] or "") for r in rows]


# --- акты ---------------------------------------------------------------------

@router.get("/{act_id}", response_model=ActOut, summary="Один акт")
def get_act(act_id: int, user: User = Depends(security.current_user)) -> ActOut:
    return _out(_load(act_id))


@router.post("", response_model=ActOut, status_code=status.HTTP_201_CREATED,
             summary="Составить акт")
def create_act(form: ActIn, user: User = Depends(security.current_user)) -> ActOut:
    _check_claims(form.claim_ids, 0)
    try:
        created = db.fetch_one(
            "INSERT INTO torg_act (number, act_year, act_date, supplier, payload,"
            "  created_by, updated_by) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)"
            " RETURNING id",
            (form.number, form.act_date.year, form.act_date, form.supplier.strip(),
             _json(form.payload), user.id, user.id))
    except errors.UniqueViolation:
        raise _duplicate(form.number, form.act_date.year) from None
    _link(created["id"], form.claim_ids)
    auditlog.record(user.id, "torg_act", created["id"], "create", {
        "number": form.number, "claims": len(form.claim_ids),
        auditlog.ABOUT: _title(form.number, form.supplier)})
    return _out(_load(created["id"]))


@router.put("/{act_id}", response_model=ActOut, summary="Изменить акт")
def update_act(act_id: int, form: ActIn,
               user: User = Depends(security.current_user)) -> ActOut:
    old = _load(act_id)
    _check_claims(form.claim_ids, act_id)
    try:
        db.execute(
            "UPDATE torg_act SET number = %s, act_year = %s, act_date = %s,"
            " supplier = %s, payload = %s::jsonb, updated_by = %s, updated_at = now()"
            " WHERE id = %s",
            (form.number, form.act_date.year, form.act_date, form.supplier.strip(),
             _json(form.payload), user.id, act_id))
    except errors.UniqueViolation:
        raise _duplicate(form.number, form.act_date.year) from None
    _link(act_id, form.claim_ids)
    changes = auditlog.diff(
        {"number": old["number"], "act_date": old["act_date"], "supplier": old["supplier"]},
        {"number": form.number, "act_date": form.act_date, "supplier": form.supplier.strip()})
    auditlog.record(user.id, "torg_act", act_id, "update",
                    {**changes, auditlog.ABOUT: _title(form.number, form.supplier)})
    return _out(_load(act_id))
