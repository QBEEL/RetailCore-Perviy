"""Привязка получателей 1С к карточкам поставщиков.

«Получатель» в выгрузке — юрлицо («НеваЛайн ООО»), карточка поставщика заведена
под торговым именем. Соответствие ставится один раз и действует для всех:
привязка общая, поэтому её видит и правит любой пользователь, а не только автор.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status

from .. import db, security
from ..schemas import RecipientLinkIn, RecipientLinkOut, UnlinkedRecipient
from ..security import User

router = APIRouter(prefix="/api/recipients", tags=["Получатели"])


@router.get("/links", response_model=list[RecipientLinkOut],
            summary="Все привязки")
def list_links(user: User = Depends(security.current_user)
               ) -> list[RecipientLinkOut]:
    return [RecipientLinkOut(**row) for row in db.fetch_all(
        "SELECT recipient_key, recipient, supplier_id, linked_by, updated_at"
        " FROM recipient_link ORDER BY recipient")]


@router.put("/links", response_model=RecipientLinkOut, summary="Привязать")
def save_link(form: RecipientLinkIn,
              user: User = Depends(security.current_user)) -> RecipientLinkOut:
    row = db.fetch_one(
        "INSERT INTO recipient_link (recipient_key, recipient, supplier_id,"
        "                            linked_by)"
        " VALUES (lower(btrim(%s)), %s, %s, 'manual')"
        " ON CONFLICT (recipient_key) DO UPDATE"
        " SET recipient = EXCLUDED.recipient,"
        "     supplier_id = EXCLUDED.supplier_id,"
        "     linked_by = 'manual', updated_at = now()"
        " RETURNING recipient_key, recipient, supplier_id, linked_by, updated_at",
        (form.recipient, form.recipient, form.supplier_id))
    # Привязка меняет и сами оплаты: без этого календарь продолжил бы считать
    # получателя непривязанным до следующего импорта.
    db.execute(
        "UPDATE payment SET supplier_id = %s"
        " WHERE recipient_key = lower(btrim(%s)) AND supplier_id <> %s",
        (form.supplier_id, form.recipient, form.supplier_id))
    db.execute(
        "INSERT INTO audit_log (user_id, entity, action, changes)"
        " VALUES (%s, 'recipient_link', 'save',"
        "         jsonb_build_object('recipient', %s::text,"
        "                            'supplier_id', %s::int))",
        (user.id, form.recipient, form.supplier_id))
    return RecipientLinkOut(**row)


@router.delete("/links/{recipient_key}",
               status_code=status.HTTP_204_NO_CONTENT, summary="Снять привязку")
def drop_link(recipient_key: str,
              user: User = Depends(security.current_user)) -> None:
    db.execute("DELETE FROM recipient_link WHERE recipient_key = %s",
               (recipient_key,))
    db.execute("UPDATE payment SET supplier_id = 0 WHERE recipient_key = %s",
               (recipient_key,))
    db.execute(
        "INSERT INTO audit_log (user_id, entity, action, changes)"
        " VALUES (%s, 'recipient_link', 'delete',"
        "         jsonb_build_object('recipient_key', %s::text))",
        (user.id, recipient_key))


@router.get("/unlinked", response_model=list[UnlinkedRecipient],
            summary="Получатели без карточки поставщика")
def unlinked(user: User = Depends(security.current_user)
             ) -> list[UnlinkedRecipient]:
    rows = db.fetch_all(
        "SELECT recipient, COUNT(*) AS payments, SUM(amount) AS amount"
        " FROM payment"
        " WHERE supplier_id = 0 AND recipient <> ''"
        " GROUP BY recipient ORDER BY SUM(amount) DESC")
    return [UnlinkedRecipient(recipient=r["recipient"],
                              payments=r["payments"],
                              amount=float(r["amount"] or 0)) for r in rows]
