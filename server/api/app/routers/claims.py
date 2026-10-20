"""Торг-2: недопоставки и браки.

Склад заводит заявку, категорийный менеджер (КМ) отвечает, закрывают оба.
Закрытые заявки — это архив: тот же список, отфильтрованный по статусу.

Права устроены проще, чем у оплат, и намеренно. У оплаты есть «чья она» из
выгрузки 1С; у заявки такого признака нет, а склад — не группа в базе, а те, кто
в этот день стоит на приёмке. Поэтому заводить и закрывать может любой вошедший,
а правят данные заявки только её участники и администратор. «Принято в работу»
ставит только тот, на кого заявка адресована, — это его ответ, и ставить его за
него нельзя. Вернуть заявку из архива — только администратор: закрытое должно
оставаться закрытым, иначе архив перестаёт быть архивом.
"""
from __future__ import annotations

import os
import secrets
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import auditlog, db, security
from ..security import User
from ..settings import settings
from .files import CHUNK, MAX_SIZE, _SAFE_EXTENSION, clean_name, is_risky

router = APIRouter(prefix="/api/claims", tags=["Торг-2"])

Kind = Literal["shortage", "defect"]
Resolution = Literal["delivered", "replaced", "refund", "written_off", "rejected"]

# Сколько закрытых заявок отдаётся за раз. Архив растёт каждый месяц, а
# открывают его ради последних; остальное ищется по тексту.
ARCHIVE_LIMIT = 500


class ClaimIn(BaseModel):
    kind: Kind
    brand: str = Field("", max_length=200)
    product: str = Field("", max_length=500)
    spec: str = Field("", max_length=300)
    barcode: str = Field("", max_length=64)
    quantity: float = Field(1, gt=0, le=1_000_000)
    expiry_date: date | None = None
    comment: str = Field("", max_length=2000)
    manager_id: int | None = None


class ClaimPatch(BaseModel):
    """Точечная правка. Не переданное поле не меняется.

    `manager_id` отличает «не трогать» от «снять КМ» по самому присутствию в
    теле запроса (см. `model_fields_set`), а не по значению.
    """

    kind: Kind | None = None
    brand: str | None = Field(None, max_length=200)
    product: str | None = Field(None, max_length=500)
    spec: str | None = Field(None, max_length=300)
    barcode: str | None = Field(None, max_length=64)
    quantity: float | None = Field(None, gt=0, le=1_000_000)
    expiry_date: date | None = None
    comment: str | None = Field(None, max_length=2000)
    manager_id: int | None = None


class CloseIn(BaseModel):
    resolution: Resolution
    note: str = Field("", max_length=1000)


class ClaimOut(BaseModel):
    id: int
    kind: Kind
    brand: str
    product: str
    spec: str
    barcode: str
    quantity: float
    expiry_date: date | None
    comment: str
    status: Literal["new", "accepted", "closed"]
    applicant_id: int
    applicant: str
    manager_id: int | None
    manager: str
    manager_seen: bool
    resolution: str
    close_note: str
    created_at: datetime
    closed_at: datetime | None
    closed_by: str
    files: int = 0
    # Акт ТОРГ-2, составленный по заявке.
    act_id: int | None = None
    act_number: int | None = None
    # Что может текущий пользователь. Считается здесь, чтобы приложение не
    # повторяло правила и не разошлось с ними при изменении.
    can_edit: bool = False
    can_accept: bool = False
    can_close: bool = False
    can_reopen: bool = False
    can_delete: bool = False


class ManagerOut(BaseModel):
    id: int
    full_name: str


class ClaimFileOut(BaseModel):
    id: int
    claim_id: int
    name: str
    size: int
    added_at: datetime


class InboxItem(BaseModel):
    id: int
    kind: Kind
    brand: str
    product: str
    applicant: str


class Inbox(BaseModel):
    # Адресованных мне и ещё не открытых.
    unseen: int
    # Адресованных мне и не закрытых — для подписи у раздела.
    open: int
    # Самые свежие непрочитанные — для текста уведомления.
    items: list[InboxItem]


class Hint(BaseModel):
    """Подсказка при заведении: что уже известно о товаре и бренде."""

    brand: str = ""
    product: str = ""
    spec: str = ""
    manager_id: int | None = None


_SELECT = (
    "SELECT c.id, c.kind, c.brand, c.product, c.spec, c.barcode, c.quantity,"
    "       c.expiry_date, c.comment, c.status, c.applicant_id,"
    "       COALESCE(NULLIF(a.full_name, ''), a.login) AS applicant,"
    "       c.manager_id,"
    "       COALESCE(NULLIF(m.full_name, ''), m.login, '') AS manager,"
    "       c.manager_seen, c.resolution, c.close_note, c.created_at,"
    "       c.closed_at,"
    "       COALESCE(NULLIF(cb.full_name, ''), cb.login, '') AS closed_by,"
    "       (SELECT COUNT(*) FROM claim_file f WHERE f.claim_id = c.id) AS files,"
    "       c.act_id, t.number AS act_number"
    " FROM claim c"
    " JOIN app_user a ON a.id = c.applicant_id"
    " LEFT JOIN app_user m ON m.id = c.manager_id"
    " LEFT JOIN app_user cb ON cb.id = c.closed_by"
    " LEFT JOIN torg_act t ON t.id = c.act_id"
)

# Название поля → как оно называется в журнале. В журнал идёт имя менеджера, а
# не номер: через год по «manager_id 17» уже ничего не понять.
_FIELDS = ("kind", "brand", "product", "spec", "barcode", "quantity",
           "expiry_date", "comment")


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                         detail="Заявка не найдена")


def _forbidden(text: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=text)


def _out(row: dict, user: User) -> ClaimOut:
    row = dict(row)
    row["quantity"] = float(row["quantity"])
    row["files"] = int(row["files"])
    closed = row["status"] == "closed"
    mine = user.id in (row["applicant_id"], row["manager_id"])
    row["can_edit"] = not closed and (mine or user.is_admin)
    # «Принято в работу» — ответ КМ. Администратор может поставить его за
    # человека в отпуске, остальные нет.
    row["can_accept"] = (row["status"] == "new"
                         and (user.id == row["manager_id"] or user.is_admin))
    row["can_close"] = not closed
    row["can_reopen"] = closed and user.is_admin
    row["can_delete"] = user.is_admin or (
        row["status"] == "new" and user.id == row["applicant_id"])
    return ClaimOut(**row)


def _load(claim_id: int) -> dict:
    row = db.fetch_one(_SELECT + " WHERE c.id = %s", (claim_id,))
    if not row:
        raise _not_found()
    return row


def _title(row: dict) -> dict[str, Any]:
    """Краткая выписка для журнала: заявку по номеру не узнать, а удалённую —
    не найти нигде."""
    name = " · ".join(part for part in (row.get("brand"), row.get("product"))
                      if part)
    return {"title": name or f"заявка {row.get('id', '')}"}


def _log(user: User, claim_id: int, action: str, row: dict,
         changes: dict[str, Any] | None = None) -> None:
    body = dict(changes or {})
    body[auditlog.ABOUT] = _title(row)
    auditlog.record(user.id, "claim", claim_id, action, body)


def _managers() -> list[dict]:
    """КМ — те, у кого задано направление.

    Отдельного признака «категорийный менеджер» в учётках нет: поставщиков ведут
    ровно те, у кого есть направление, и второй признак рано или поздно
    разошёлся бы с первым (см. `routers/users.py`).
    """
    return db.fetch_all(
        "SELECT u.id, COALESCE(NULLIF(u.full_name, ''), u.login) AS full_name"
        " FROM app_user u WHERE u.is_active"
        "  AND EXISTS (SELECT 1 FROM user_direction d WHERE d.user_id = u.id)"
        " ORDER BY 2")


def _check_manager(manager_id: int | None) -> None:
    if manager_id is None:
        return
    if manager_id not in {row["id"] for row in _managers()}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Этот человек не категорийный менеджер: "
                   "у его учётной записи не задано направление")


# --- справочное: то, что не зависит от номера заявки --------------------------

@router.get("/managers", response_model=list[ManagerOut],
            summary="Категорийные менеджеры")
def managers(user: User = Depends(security.current_user)) -> list[ManagerOut]:
    return [ManagerOut(**row) for row in _managers()]


@router.get("/inbox", response_model=Inbox, summary="Что адресовано мне")
def inbox(user: User = Depends(security.current_user)) -> Inbox:
    """Опрашивается приложением раз в минуту — запрос должен быть дешёвым."""
    counts = db.fetch_one(
        "SELECT COUNT(*) FILTER (WHERE NOT manager_seen) AS unseen,"
        "       COUNT(*) AS open"
        " FROM claim WHERE manager_id = %s AND status <> 'closed'", (user.id,))
    fresh = db.fetch_all(
        "SELECT c.id, c.kind, c.brand, c.product,"
        "       COALESCE(NULLIF(a.full_name, ''), a.login) AS applicant"
        " FROM claim c JOIN app_user a ON a.id = c.applicant_id"
        " WHERE c.manager_id = %s AND c.status <> 'closed' AND NOT c.manager_seen"
        " ORDER BY c.created_at DESC, c.id DESC LIMIT 5", (user.id,))
    return Inbox(unseen=int(counts["unseen"] or 0), open=int(counts["open"] or 0),
                 items=[InboxItem(**row) for row in fresh])


@router.get("/hint", response_model=Hint, summary="Подсказка по штрих-коду и бренду")
def hint(user: User = Depends(security.current_user),
         barcode: str = "", brand: str = "") -> Hint:
    """Штрих-код уже заводили — подставляем бренд, название и КМ из последней
    заявки. Иначе по бренду берём КМ, которому его адресовали в прошлый раз:
    склад часто не знает, чей это бренд, а на деле он почти всегда один и тот
    же человек."""
    barcode, brand = barcode.strip(), brand.strip()
    if barcode:
        row = db.fetch_one(
            "SELECT brand, product, spec, manager_id FROM claim"
            " WHERE barcode = %s ORDER BY id DESC LIMIT 1", (barcode,))
        if row:
            return Hint(**row)
    if brand:
        row = db.fetch_one(
            "SELECT manager_id FROM claim WHERE lower(brand) = lower(%s)"
            "  AND manager_id IS NOT NULL ORDER BY id DESC LIMIT 1", (brand,))
        if row:
            return Hint(manager_id=row["manager_id"])
    return Hint()


# --- заявки -------------------------------------------------------------------

@router.get("", response_model=list[ClaimOut], summary="Список заявок")
def list_claims(user: User = Depends(security.current_user),
                archive: bool = False, mine: bool = False,
                text: str = "") -> list[ClaimOut]:
    where, values = [("c.status = 'closed'" if archive else "c.status <> 'closed'")], []
    if mine:
        where.append("(c.manager_id = %s OR c.applicant_id = %s)")
        values += [user.id, user.id]
    if text.strip():
        like = f"%{text.strip()}%"
        where.append("(c.brand ILIKE %s OR c.product ILIKE %s OR c.barcode ILIKE %s"
                     " OR c.comment ILIKE %s)")
        values += [like] * 4
    order = ("c.closed_at DESC NULLS LAST, c.id DESC" if archive
             else "c.created_at DESC, c.id DESC")
    limit = f" LIMIT {ARCHIVE_LIMIT}" if archive else ""
    rows = db.fetch_all(
        f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY {order}{limit}", values)
    return [_out(row, user) for row in rows]


@router.post("", response_model=ClaimOut, status_code=status.HTTP_201_CREATED,
             summary="Завести заявку")
def create_claim(form: ClaimIn,
                 user: User = Depends(security.current_user)) -> ClaimOut:
    if not (form.brand.strip() or form.product.strip() or form.barcode.strip()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Укажите хотя бы бренд, название или штрих-код")
    _check_manager(form.manager_id)
    # Назначил сам себя — читать об этом уведомление незачем.
    seen = form.manager_id in (None, user.id)
    created = db.fetch_one(
        "INSERT INTO claim (kind, brand, product, spec, barcode, quantity,"
        "  expiry_date, comment, applicant_id, manager_id, manager_seen)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (form.kind, form.brand.strip(), form.product.strip(), form.spec.strip(),
         form.barcode.strip(), form.quantity, form.expiry_date,
         form.comment.strip(), user.id, form.manager_id, seen))
    row = _load(created["id"])
    _log(user, created["id"], "create", row, {
        "kind": form.kind, "quantity": form.quantity, "manager": row["manager"]})
    return _out(row, user)


@router.get("/{claim_id}", response_model=ClaimOut, summary="Одна заявка")
def get_claim(claim_id: int,
              user: User = Depends(security.current_user)) -> ClaimOut:
    return _out(_load(claim_id), user)


@router.patch("/{claim_id}", response_model=ClaimOut, summary="Изменить заявку")
def patch_claim(claim_id: int, form: ClaimPatch,
                user: User = Depends(security.current_user)) -> ClaimOut:
    old = _load(claim_id)
    if not _out(old, user).can_edit:
        raise _forbidden("Закрытую заявку не правят, а чужую правят только "
                         "заявитель, КМ и администратор")

    values = {name: getattr(form, name) for name in _FIELDS
              if name in form.model_fields_set and getattr(form, name) is not None}
    # Пустая дата — «срока годности нет»: отличается от «не менять» тем, что
    # поле передано явно.
    if "expiry_date" in form.model_fields_set:
        values["expiry_date"] = form.expiry_date
    for name in ("brand", "product", "spec", "barcode", "comment"):
        if isinstance(values.get(name), str):
            values[name] = values[name].strip()

    sets, params = [], []
    for name, value in values.items():
        sets.append(f"{name} = %s")
        params.append(value)

    changes = auditlog.diff(
        {name: old[name] for name in values},
        {name: value for name, value in values.items()})

    manager_changed = ("manager_id" in form.model_fields_set
                       and form.manager_id != old["manager_id"])
    if manager_changed:
        _check_manager(form.manager_id)
        # Уведомление уходит тому, кого назначили, — если это не тот, кто
        # назначает: читать сообщение о собственном действии незачем.
        sets += ["manager_id = %s", "manager_seen = %s"]
        params += [form.manager_id, form.manager_id in (None, user.id)]
        # Новому КМ заявка начинается с «Новая»: прежний ответ был чужим.
        if old["status"] == "accepted":
            sets.append("status = 'new'")

    if not sets:
        return _out(old, user)

    db.execute(f"UPDATE claim SET {', '.join(sets)}, updated_at = now()"
               " WHERE id = %s", [*params, claim_id])
    row = _load(claim_id)
    if manager_changed:
        # В журнал — имена, а не номера учёток.
        changes = {**changes, "manager": row["manager"], auditlog.WAS: {
            **changes.get(auditlog.WAS, {}), "manager": old["manager"]}}
    _log(user, claim_id, "update", row, changes)
    return _out(row, user)


@router.post("/{claim_id}/seen", response_model=dict,
             summary="КМ открыл заявку")
def mark_seen(claim_id: int,
              user: User = Depends(security.current_user)) -> dict:
    """Снимает «новое» у адресата. Чужое открытие ничего не значит: заявку
    мог посмотреть администратор, а КМ о ней всё ещё не знает."""
    changed = db.execute(
        "UPDATE claim SET manager_seen = TRUE"
        " WHERE id = %s AND manager_id = %s AND NOT manager_seen",
        (claim_id, user.id))
    return {"changed": changed}


@router.post("/{claim_id}/accept", response_model=ClaimOut,
             summary="Принято в работу")
def accept(claim_id: int,
           user: User = Depends(security.current_user)) -> ClaimOut:
    old = _load(claim_id)
    if not _out(old, user).can_accept:
        raise _forbidden("Принять в работу может только КМ, на которого "
                         "адресована заявка, или администратор")
    db.execute("UPDATE claim SET status = 'accepted', manager_seen = TRUE,"
               " updated_at = now() WHERE id = %s", (claim_id,))
    row = _load(claim_id)
    _log(user, claim_id, "accept", row)
    return _out(row, user)


@router.post("/{claim_id}/close", response_model=ClaimOut, summary="Закрыть")
def close(claim_id: int, form: CloseIn,
          user: User = Depends(security.current_user)) -> ClaimOut:
    old = _load(claim_id)
    if old["status"] == "closed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Заявка уже закрыта")
    db.execute(
        "UPDATE claim SET status = 'closed', resolution = %s, close_note = %s,"
        " closed_at = now(), closed_by = %s, manager_seen = TRUE,"
        " updated_at = now() WHERE id = %s",
        (form.resolution, form.note.strip(), user.id, claim_id))
    row = _load(claim_id)
    _log(user, claim_id, "close", row, {"resolution": form.resolution,
                                        "note": form.note.strip()})
    return _out(row, user)


@router.post("/{claim_id}/reopen", response_model=ClaimOut,
             summary="Вернуть из архива")
def reopen(claim_id: int,
           user: User = Depends(security.admin_only)) -> ClaimOut:
    old = _load(claim_id)
    if old["status"] != "closed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Заявка не в архиве")
    # Возвращается в «Принято в работу», а не в «Новая»: заявку уже разбирали,
    # и КМ получает уведомление, что к ней нужно вернуться. Итог закрытия
    # очищается: он относился к закрытию, а не к заявке.
    db.execute(
        "UPDATE claim SET status = 'accepted', resolution = '', close_note = '',"
        " closed_at = NULL, closed_by = NULL, manager_seen = FALSE,"
        " updated_at = now() WHERE id = %s", (claim_id,))
    row = _load(claim_id)
    _log(user, claim_id, "reopen", row, {"was_resolution": old["resolution"]})
    return _out(row, user)


@router.delete("/{claim_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Удалить заявку")
def delete_claim(claim_id: int,
                 user: User = Depends(security.current_user)) -> None:
    old = _load(claim_id)
    if not _out(old, user).can_delete:
        raise _forbidden("Удалить можно только свою заявку, на которую ещё не "
                         "ответили. Остальное закрывается, а не удаляется")
    names = db.fetch_all("SELECT stored_as FROM claim_file WHERE claim_id = %s",
                         (claim_id,))
    db.execute("DELETE FROM claim WHERE id = %s", (claim_id,))
    folder = _folder(claim_id)
    for item in names:
        try:
            os.remove(os.path.join(folder, item["stored_as"]))
        except OSError:
            pass
    _log(user, claim_id, "delete", old)


# --- вложения -----------------------------------------------------------------

def _folder(claim_id: int) -> str:
    # Отдельная папка от вложений оплат: номера оплат и заявок пересекаются.
    return os.path.join(settings.files_dir, "claims", str(claim_id))


@router.get("/{claim_id}/files", response_model=list[ClaimFileOut],
            summary="Вложения заявки")
def list_files(claim_id: int,
               user: User = Depends(security.current_user)) -> list[ClaimFileOut]:
    return [ClaimFileOut(**row) for row in db.fetch_all(
        "SELECT id, claim_id, name, size, added_at FROM claim_file"
        " WHERE claim_id = %s ORDER BY added_at", (claim_id,))]


@router.post("/{claim_id}/files", response_model=ClaimFileOut,
             status_code=status.HTTP_201_CREATED, summary="Приложить файл")
async def upload(claim_id: int, file: UploadFile,
                 user: User = Depends(security.current_user)) -> ClaimFileOut:
    claim = _load(claim_id)
    # Закрытую заявку не дополняют, но склад часто довозит акт после ответа КМ —
    # это открытая заявка. В архиве всё уже решено.
    if claim["status"] == "closed":
        raise _forbidden("Заявка закрыта — файлы к ней уже не добавляются")

    name = clean_name(file.filename)
    if is_risky(name):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Программы и сценарии прикладывать нельзя — только документы")
    extension = os.path.splitext(name)[1]
    stored = secrets.token_hex(16) + (
        extension if _SAFE_EXTENSION.match(extension) else "")
    folder = _folder(claim_id)
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
        if os.path.exists(target):
            os.remove(target)
        raise

    row = db.fetch_one(
        "INSERT INTO claim_file (claim_id, name, stored_as, size, added_by)"
        " VALUES (%s, %s, %s, %s, %s)"
        " RETURNING id, claim_id, name, size, added_at",
        (claim_id, name or stored, stored, size, user.id))
    _log(user, claim_id, "attach", claim, {"file": name or stored})
    return ClaimFileOut(**row)


@router.get("/files/{file_id}", summary="Скачать вложение")
def download(file_id: int,
             user: User = Depends(security.current_user)) -> FileResponse:
    row = db.fetch_one(
        "SELECT claim_id, name, stored_as FROM claim_file WHERE id = %s",
        (file_id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Вложение не найдено")
    path = os.path.join(_folder(row["claim_id"]), row["stored_as"])
    if not os.path.isfile(path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Файл потерян: есть в базе, но не на диске")
    return FileResponse(path, filename=clean_name(row["name"]) or row["stored_as"])


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Убрать вложение")
def detach(file_id: int, user: User = Depends(security.current_user)) -> None:
    row = db.fetch_one(
        "SELECT f.claim_id, f.stored_as, f.name, f.added_by, c.status"
        " FROM claim_file f JOIN claim c ON c.id = f.claim_id"
        " WHERE f.id = %s", (file_id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Вложение не найдено")
    if row["status"] == "closed":
        raise _forbidden("Заявка закрыта — файлы из неё не убирают")
    claim = _load(row["claim_id"])
    # Убрать файл может тот, кто его приложил, участник заявки или администратор.
    if not (user.is_admin or user.id == row["added_by"]
            or user.id in (claim["applicant_id"], claim["manager_id"])):
        raise _forbidden("Файл приложил другой человек")

    db.execute("DELETE FROM claim_file WHERE id = %s", (file_id,))
    _log(user, row["claim_id"], "detach", claim, {"file": row["name"]})
    try:
        os.remove(os.path.join(_folder(row["claim_id"]), row["stored_as"]))
    except OSError:
        pass
