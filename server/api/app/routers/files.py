"""Вложения к оплатам: список, загрузка, скачивание, удаление.

Файлы лежат в томе сервера, а в базе хранится только имя. Имя в хранилище
генерируется, а не берётся из загрузки: два человека могут прислать «счёт.pdf»,
и одно имя затёрло бы другое. Настоящее имя показывается пользователю из базы.
"""
from __future__ import annotations

import os
import re
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

# Типы, которые система запускает, а не показывает: как вложения не нужны.
# Тот же набор и та же очистка имени — в клиенте (`app/core/payments/safe_files.py`).
RISKY_EXTENSIONS = frozenset({
    ".exe", ".com", ".scr", ".pif", ".msi", ".msp", ".dll", ".cpl", ".sys",
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf",
    ".wsh", ".hta", ".lnk", ".url", ".reg", ".jar", ".msc", ".chm", ".appx",
    ".gadget", ".application", ".sh", ".command", ".app", ".dmg", ".apk",
})
_FORBIDDEN = '<>:"|?*'
MAX_NAME = 150
_SAFE_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


def clean_name(raw: str | None) -> str:
    """Имя без путей, управляющих знаков и запрещённых символов.

    Имя приходит от клиента как есть и потом уезжает другим клиентам, которые
    складывают файл на диск под этим именем: путь в нём нужно отбросить.
    """
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in _FORBIDDEN)
    name = name.strip(" .")
    if len(name) > MAX_NAME:
        root, ext = os.path.splitext(name)
        name = root[:MAX_NAME - len(ext)] + ext
    return name


def is_risky(name: str) -> bool:
    """Запускаемый тип — по любому расширению в имени, не только по последнему."""
    return any(f".{part.strip()}" in RISKY_EXTENSIONS
               for part in name.lower().split(".")[1:])


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

    name = clean_name(file.filename)
    if is_risky(name):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Программы и сценарии прикладывать нельзя — только документы")
    extension = os.path.splitext(name)[1]
    stored = secrets.token_hex(16) + (
        extension if _SAFE_EXTENSION.match(extension) else "")
    folder = os.path.join(settings.files_dir, str(payment_id))
    os.makedirs(folder, exist_ok=True)
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
        (payment_id, name or stored, stored, size, user.id))
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
    # Имена, приложенные до очистки при загрузке, могли быть любыми.
    return FileResponse(path, filename=clean_name(row["name"]) or row["stored_as"])


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
