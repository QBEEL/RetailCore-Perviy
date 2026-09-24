"""Удаление данных общей базы.

Оплаты приходят из 1С прогоном целиком, и выгрузку случается перенести заново —
после смены нумерации документов или правки самой выгрузки, когда расходится не
отдельная запись, а весь набор. Дописывать такую выгрузку к накопленному
бессмысленно: сойдётся она только с пустой базой.

Стираются оплаты и то, что живёт при них: вложения, бюджеты, журнал импортов и
привязки получателей. Учётные записи, направления, закрепление поставщиков и
журнал изменений остаются — восстанавливать их руками дороже, чем повторить
импорт, а запись о самом удалении попадает в тот же журнал и должна там
уцелеть.

Подтверждение двойное: слово в диалоге и пароль. Слово защищает от промаха
мышью, пароль — от чужих рук за незапертым компьютером: токен живёт двенадцать
часов, а пароля в нём нет.
"""
from __future__ import annotations

import json
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from .. import db, security
from ..security import User
from ..settings import settings

router = APIRouter(prefix="/api/maintenance", tags=["Обслуживание"])

# Слово подтверждения. Набирается вручную: «Да» в обычном вопросе нажимается
# не глядя, а это действие нечем отменить.
CONFIRM = "УДАЛИТЬ"

# Что стирается, в порядке удаления. Вложения раньше оплат: с обратным порядком
# каскад удалил бы их сам и сосчитать стёртое было бы нечем.
#
# DELETE, а не TRUNCATE: счётчики идентификаторов должны продолжиться с прежнего
# места. Скачанные вложения кэшируются у менеджеров по номеру оплаты, и после
# сброса счётчика новая оплата открывала бы чужой файл из кэша.
TABLES: tuple[tuple[str, str], ...] = (
    ("files", "payment_file"),
    ("payments", "payment"),
    ("budgets", "budget"),
    ("imports", "import_run"),
    ("links", "recipient_link"),
)

# Счётчики одним запросом, из того же списка: два перечисления таблиц рано или
# поздно разошлись бы, и диалог обещал бы не то, что стирается.
_COUNTS = "SELECT " + ", ".join(f"(SELECT COUNT(*) FROM {table}) AS {field}"
                               for field, table in TABLES)


class Scope(BaseModel):
    """Сколько чего будет стёрто — и сколько стёрлось."""

    payments: int = 0
    files: int = 0
    budgets: int = 0
    imports: int = 0
    links: int = 0


class WipeIn(BaseModel):
    confirm: str = ""
    password: str = ""


@router.get("/scope", response_model=Scope, summary="Что будет стёрто")
def scope(user: User = Depends(security.admin_only)) -> Scope:
    return Scope(**db.fetch_one(_COUNTS))


@router.post("/wipe", response_model=Scope, summary="Удалить базу данных")
def wipe(form: WipeIn, user: User = Depends(security.admin_only)) -> Scope:
    if form.confirm.strip().upper() != CONFIRM:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Для подтверждения наберите «{CONFIRM}»")

    row = db.fetch_one("SELECT password_hash FROM app_user WHERE id = %s",
                       (user.id,))
    if not row or not security.verify_password(form.password,
                                              row["password_hash"]):
        # Неудачная попытка остаётся в журнале. Опечатка администратора выглядит
        # там безобидно, а вот попытка с чужого незапертого компьютера — это то
        # единственное место, где её потом можно будет увидеть.
        db.execute(
            "INSERT INTO audit_log (user_id, entity, action)"
            " VALUES (%s, 'database', 'wipe_denied')", (user.id,))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Пароль указан неверно")

    # Папки вложений — до удаления: после него связь файла на диске с оплатой
    # существует только здесь, в этом списке.
    folders = [int(item["payment_id"]) for item in db.fetch_all(
        "SELECT DISTINCT payment_id FROM payment_file")]

    # Одной транзакцией: половина стёртой базы хуже и целой, и пустой.
    with db.cursor() as handle:
        erased: dict[str, int] = {}
        for field, table in TABLES:
            handle.execute(f"DELETE FROM {table}")
            erased[field] = handle.rowcount
        handle.execute(
            "INSERT INTO audit_log (user_id, entity, action, changes)"
            " VALUES (%s, 'database', 'wipe', %s)",
            (user.id, json.dumps(erased)))

    _drop_files(folders)
    return Scope(**erased)


def _drop_files(payment_ids: list[int]) -> None:
    """Убирает вложения стёртых оплат с диска.

    После базы, а не до неё: сорванная транзакция оставила бы записи об уже
    удалённых файлах, а это хуже забытой на диске папки — её не видно ни в
    одном разделе, тогда как вложение без файла пользователь встретит при
    первой же попытке его открыть.
    """
    for payment_id in payment_ids:
        try:
            shutil.rmtree(os.path.join(settings.files_dir, str(payment_id)))
        except OSError:
            # Записи из базы уже убраны; недоступная папка — не повод отвечать
            # ошибкой на удаление, которое состоялось.
            pass
