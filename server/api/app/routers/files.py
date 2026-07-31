"""Вложения к оплатам: список, загрузка, скачивание, удаление.

Файлы лежат в томе сервера, а в базе хранится только имя. Имя в хранилище
генерируется, а не берётся из загрузки: два человека могут прислать «счёт.pdf»,
и одно имя затёрло бы другое. Настоящее имя показывается пользователю из базы.
"""
from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from .. import db, security
from ..schemas import FileOut
from ..security import User
from ..settings import settings

router = APIRouter(prefix="/api/payments", tags=["Вложения"])

# Ограничение размера. Счёт или акт в это укладывается с запасом, а случайно
# выбранный архив на сотни мегабайт забил бы диск, который делится с соседом.
MAX_SIZE = 25 * 1024 * 1024
CHUNK = 1024 * 1024


def _owner(payment_id: int) -> str:
    row = db.fetch_one("SELECT responsible FROM payment WHERE id = %s",
                       (payment_id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Оплата не найдена")
    return row["responsible"]


@router.get("/{payment_id}/files", response_model=list[FileOut],
            summary="Вложения оплаты")
def list_files(payment_id: int,
               user: User = Depends(security.current_user)) -> list[FileOut]:
    return [FileOut(**row) for row in db.fetch_all(
        "SELECT id, payment_id, name, size, added_at FROM payment_file"
        " WHERE payment_id = %s ORDER BY added_at", (payment_id,))]


@router.post("/{payment_id}/files", response_model=FileOut,
             status_code=status.HTTP_201_CREATED, summary="Приложить файл")
async def upload(payment_id: int, file: UploadFile,
                 user: User = Depends(security.current_user)) -> FileOut:
    if not user.may_edit(_owner(payment_id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Оплата закреплена за другим менеджером")

    folder = os.path.join(settings.files_dir, str(payment_id))
    os.makedirs(folder, exist_ok=True)
    stored = secrets.token_hex(16) + os.path.splitext(file.filename or "")[1]
    target = os.path.join(folder, stored)

    size = 0
    try:
        with open(target, "wb") as handle:
            while chunk := await file.read(CHUNK):
                size += len(chunk)
                if size > MAX_SIZE:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Файл больше {MAX_SIZE // 1024 // 1024} МБ")
                handle.write(chunk)
    except Exception:
        # Недописанный файл на диске не нужен никому: запись в базу не
        # состоялась, и найти его потом было бы нечем.
        if os.path.exists(target):
            os.remove(target)
        raise

    row = db.fetch_one(
        "INSERT INTO payment_file (payment_id, name, stored_as, size, added_by)"
        " VALUES (%s, %s, %s, %s, %s)"
        " RETURNING id, payment_id, name, size, added_at",
        (payment_id, file.filename or stored, stored, size, user.id))
    db.execute("UPDATE payment SET had_files = TRUE WHERE id = %s",
               (payment_id,))
    return FileOut(**row)


@router.get("/files/{file_id}", summary="Скачать вложение")
def download(file_id: int, user: User = Depends(security.current_user)
             ) -> FileResponse:
    row = db.fetch_one(
        "SELECT payment_id, name, stored_as FROM payment_file WHERE id = %s",
        (file_id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Вложение не найдено")
    path = os.path.join(settings.files_dir, str(row["payment_id"]),
                        row["stored_as"])
    if not os.path.isfile(path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Файл потерян: есть в базе, но не на диске")
    return FileResponse(path, filename=row["name"])


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Убрать вложение")
def detach(file_id: int, user: User = Depends(security.current_user)) -> None:
    row = db.fetch_one(
        "SELECT f.payment_id, f.stored_as, p.responsible"
        " FROM payment_file f JOIN payment p ON p.id = f.payment_id"
        " WHERE f.id = %s", (file_id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Вложение не найдено")
    if not user.may_edit(row["responsible"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Оплата закреплена за другим менеджером")

    db.execute("DELETE FROM payment_file WHERE id = %s", (file_id,))
    path = os.path.join(settings.files_dir, str(row["payment_id"]),
                        row["stored_as"])
    try:
        os.remove(path)
    except OSError:
        # Запись из базы уже убрана; отсутствующий файл — не повод для ошибки.
        pass
