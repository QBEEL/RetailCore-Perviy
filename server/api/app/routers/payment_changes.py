"""Bounded, replayable payment deltas; no long-lived database connections."""
from __future__ import annotations

import hashlib
import json
from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import db, security
from ..security import User
from .payments import COLUMNS, _conditions, _out

router = APIRouter(prefix="/api/payments/changes", tags=["Оплаты"])
RETENTION = 50000


class Selection(BaseModel):
    text: str = ""
    start: date | None = None
    end: date | None = None
    statuses: list[str] = []
    origins: list[str] = []
    supplier_id: int = 0
    recipient_key: str = ""
    amount_from: float | None = None
    amount_to: float | None = None
    responsible: str = ""
    operation: str = ""
    over_limit: bool | None = None
    dated_only: bool = False
    dds_item: str = ""


class ChangesIn(BaseModel):
    epoch: str
    after: int = Field(ge=0)
    access: str
    selection: Selection = Field(default_factory=Selection)
    limit: int = Field(default=1000, ge=1, le=5000)


def access_key(user: User) -> str:
    # Time-limited cover rights can change at midnight without any DB write.
    value = [user.id, user.is_admin, sorted(user.mine)]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


@router.get("/head")
def head(user: User = Depends(security.current_user)) -> dict:
    row = db.fetch_one("SELECT epoch, revision FROM payment_sync_state WHERE singleton")
    return {"epoch": str(row["epoch"]), "revision": row["revision"], "access": access_key(user)}


@router.post("")
def changes(form: ChangesIn, user: User = Depends(security.current_user)) -> dict:
    # Watermark, events and rows must come from the SAME MVCC snapshot. Otherwise
    # a new commit between queries could cause a client to advance past unseen data.
    with db.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute("SELECT epoch, revision FROM payment_sync_state WHERE singleton")
        state = cursor.fetchone()
        epoch, latest = str(state["epoch"]), state["revision"]
        access = access_key(user)
        result = {"epoch": epoch, "revision": latest, "access": access,
                  "upserted": [], "removed_ids": [], "metadata": False,
                  "reset": False, "more": False}
        if (form.epoch != epoch or form.access != access or form.after > latest
                or form.after < max(0, latest - RETENTION)):
            return {**result, "reset": True}
        cursor.execute(
            "SELECT revision, payment_id, metadata, reset FROM payment_change"
            " WHERE revision > %s ORDER BY revision LIMIT %s", (form.after, form.limit))
        events = cursor.fetchall()
        if not events:
            return result
        if any(event["reset"] for event in events):
            return {**result, "reset": True}
        through = events[-1]["revision"]
        ids = sorted({event["payment_id"] for event in events if event["payment_id"] is not None})
        selection = form.selection
        where, values = _conditions(
            selection.text, selection.start, selection.end, selection.statuses,
            selection.origins, selection.supplier_id, selection.recipient_key,
            selection.amount_from, selection.amount_to, selection.responsible,
            selection.operation, selection.over_limit, selection.dated_only, selection.dds_item)
        rows = []
        if ids:
            cursor.execute(f"SELECT {COLUMNS} FROM payment p WHERE p.id = ANY(%s) AND ({where})",
                           [ids, *values])
            rows = [_out(row, user) for row in cursor.fetchall()]
        found = {row.id for row in rows}
        return {**result, "revision": through, "more": through < latest,
                "upserted": rows, "removed_ids": [ident for ident in ids if ident not in found],
                "metadata": any(event["metadata"] for event in events)}
