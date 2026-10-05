"""Импорт выгрузки 1С и его журнал.

Выгрузку разбирает приложение: там лежит вся логика распознавания колонок и
статусов, проверенная на тысячах строк, и переносить её на сервер незачем.
Сервер отвечает за две вещи, которые может сделать только он: отдать снимок
того, что уже лежит в базе, и записать разобранное одной транзакцией.

Импорт доступен только администратору. Он перезаписывает поля во всех оплатах
отдела, включая чужие, — это не та операция, которую каждый делает у себя.

Отдельный префикс, а не ветка внутри оплат, — чтобы `/api/imports/last` не
спорил с `/api/payments/{id}`.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import db, security
from ..schemas import Origin, Status
from ..security import User

router = APIRouter(prefix="/api/imports", tags=["Импорт"])

# Поля, которые приходят из выгрузки и потому обновляются при повторном
# импорте. Список повторяет IMPORTED_FIELDS приложения — расходиться им нельзя,
# иначе импорт затрёт то, что человек правил руками.
IMPORTED_FIELDS: tuple[str, ...] = (
    "pay_date", "amount", "vat", "currency", "recipient", "recipient_key",
    "source_status", "paid_flag", "operation", "over_limit", "priority",
    "edo_state", "responsible", "author", "had_files",
)

# Все поля новой записи.
INSERT_FIELDS: tuple[str, ...] = (
    "doc_number", "request_date", "status", "supplier_id", "comment",
    "origin", "origin_ref", *IMPORTED_FIELDS,
)


class ImportRun(BaseModel):
    id: int
    path: str
    file_hash: str
    rows_total: int
    rows_new: int
    rows_updated: int
    rows_same: int
    rows_skipped: int
    error: str
    finished_at: datetime
    started_by: str = ""


_SELECT = (
    "SELECT r.id, r.path, r.file_hash, r.rows_total, r.rows_new,"
    "       r.rows_updated, r.rows_same, r.rows_skipped, r.error,"
    "       r.finished_at, COALESCE(u.full_name, '') AS started_by"
    " FROM import_run r LEFT JOIN app_user u ON u.id = r.started_by"
)


class ExistingRow(BaseModel):
    """Снимок записи из выгрузки — чтобы клиент посчитал изменения до записи."""

    id: int
    doc_number: str
    request_date: date | None
    origin: str
    status: str
    # Есть ли ручной комментарий. Само содержимое не отдаётся: клиенту оно не
    # нужно, а лишние мегабайты по сети — нужны ещё меньше.
    manual: bool
    values: dict[str, Any]
    # Сумма до ручной правки. Клиенту нужна для предпросмотра: пока 1С
    # присылает её же, правка человека остаётся, и строка не «изменится».
    amount_before: float | None = None
    # Пометку поставила сама выгрузка, а не человек: вернуть в 1С прежнюю
    # сумму — обычное новое значение, защищать там нечего.
    amount_by_import: bool = False


class ImportPayment(BaseModel):
    """Одна разобранная строка выгрузки."""

    doc_number: str = ""
    request_date: date | None = None
    pay_date: date | None = None
    amount: float = 0.0
    vat: float = 0.0
    currency: str = "руб."
    supplier_id: int = 0
    recipient: str = ""
    recipient_key: str = ""
    status: Status = "planned"
    source_status: str = ""
    paid_flag: bool = False
    operation: str = ""
    over_limit: bool = False
    priority: str = ""
    edo_state: str = ""
    responsible: str = ""
    author: str = ""
    comment: str = ""
    had_files: bool = False
    origin: Origin = "import"
    origin_ref: str = ""


class ImportChange(BaseModel):
    id: int
    payment: ImportPayment


class ImportApply(BaseModel):
    # Полная выгрузка — около семи тысяч строк. Предел с запасом, чтобы
    # случайно поданный не тот файл не превратился в бесконечную транзакцию.
    created: list[ImportPayment] = Field(default=[], max_length=20000)
    changed: list[ImportChange] = Field(default=[], max_length=20000)
    # Новые заявки, занимающие место ручной или плановой оплаты того же
    # получателя на тот же день. Пару подбирает клиент (`importer.adoptions`).
    adopted: list[ImportChange] = Field(default=[], max_length=20000)


class ImportResult(BaseModel):
    new: int
    updated: int
    adopted: int = 0


# Что заявка приносит в занятую запись сверх полей выгрузки. Комментарий,
# вложения, карточка поставщика и метка плана остаются прежними.
ADOPTED_FIELDS: tuple[str, ...] = (
    "doc_number", "request_date", "status", "origin", *IMPORTED_FIELDS,
)

OPEN_STATUSES: tuple[str, ...] = ("planned", "overdue", "moved")


class Candidate(BaseModel):
    """Открытая оплата без номера 1С — её может занять новая заявка."""

    id: int
    pay_date: date
    amount: float
    recipient: str
    status: Status
    origin: str
    origin_ref: str


@router.get("/candidates", response_model=list[Candidate],
            summary="Оплаты, которые может занять заявка 1С")
def candidates(user: User = Depends(security.admin_only)) -> list[Candidate]:
    rows = db.fetch_all(
        "SELECT id, pay_date, amount, recipient, status, origin, origin_ref"
        " FROM payment WHERE doc_number = '' AND pay_date IS NOT NULL"
        " AND status = ANY(%s)", (list(OPEN_STATUSES),))
    return [Candidate(**row) for row in rows]


@router.get("/index", response_model=list[ExistingRow],
            summary="Снимок записей из прошлых выгрузок")
def existing_index(user: User = Depends(security.admin_only)) -> list[ExistingRow]:
    names = ", ".join(IMPORTED_FIELDS)
    rows = db.fetch_all(
        f"SELECT id, doc_number, request_date, origin, status,"
        f"       (comment <> '') AS manual, amount_before,"
        f"       (amount_before IS NOT NULL AND amount_changed_by IS NULL)"
        f"         AS amount_by_import, {names}"
        " FROM payment WHERE doc_number <> ''")
    return [
        ExistingRow(
            id=row["id"], doc_number=row["doc_number"],
            request_date=row["request_date"], origin=row["origin"],
            status=row["status"], manual=row["manual"],
            values={name: row[name] for name in IMPORTED_FIELDS},
            amount_before=(float(row["amount_before"])
                           if row["amount_before"] is not None else None),
            amount_by_import=row["amount_by_import"],
        )
        for row in rows
    ]


@router.post("/apply", response_model=ImportResult,
             summary="Записать разобранную выгрузку")
def apply_import(form: ImportApply,
                 user: User = Depends(security.admin_only)) -> ImportResult:
    """Одной транзакцией: либо применяется вся выгрузка, либо ничего.

    Изменившимся обновляются только поля из 1С — комментарий, вложения и
    назначенный вручную статус остаются на месте.
    """
    adopted = 0
    with db.cursor() as handle:
        if form.adopted:
            # Плановая сумма уходит в «было», если заявка принесла другую, —
            # так видно, что и в какую сторону заменил импорт. Прежняя пометка
            # о ручной правке к новой сумме не относится. Пустой номер в
            # условии — защита от гонки: запись могли занять между разбором
            # и подтверждением. Правая часть SET видит строку до изменения.
            assignments = ", ".join(f"{name} = %({name})s" for name in ADOPTED_FIELDS)
            for item in form.adopted:
                handle.execute(
                    f"UPDATE payment SET {assignments},"
                    f" amount_before = CASE WHEN {_SAME} THEN NULL ELSE amount END,"
                    " amount_changed_by = NULL,"
                    f" amount_changed_at = CASE WHEN {_SAME} THEN NULL ELSE now() END,"
                    " updated_at = now(), updated_by = %(user)s"
                    " WHERE id = %(id)s AND doc_number = ''",
                    {**dict(zip(ADOPTED_FIELDS, _row(item.payment, ADOPTED_FIELDS))),
                     "user": user.id, "id": item.id})
                adopted += handle.rowcount

        if form.created:
            columns = ", ".join(INSERT_FIELDS)
            marks = ", ".join(["%s"] * len(INSERT_FIELDS))
            handle.executemany(
                f"INSERT INTO payment ({columns}, updated_by)"
                f" VALUES ({marks}, %s)"
                # Тот же файл могли залить дважды: повтор не должен падать на
                # уникальном ключе и рвать всю транзакцию.
                " ON CONFLICT (doc_number, request_date)"
                "   WHERE doc_number <> '' DO NOTHING",
                [[*_row(item, INSERT_FIELDS), user.id] for item in form.created])

        if form.changed:
            assignments = ", ".join(
                [f"{name} = %({name})s" for name in IMPORTED_FIELDS
                 if name != "amount"] + [IMPORTED_AMOUNT])
            # Статус импорт не переписывает — его могли назначить вручную. Кроме
            # одного случая: 1С говорит «оплачена», а факт оплаты сильнее статуса.
            assignments += ", status = CASE WHEN %(paid_flag)s THEN 'paid' ELSE status END"
            handle.executemany(
                f"UPDATE payment SET {assignments}, updated_at = now(),"
                " updated_by = %(user)s WHERE id = %(id)s",
                [{**dict(zip(IMPORTED_FIELDS, _row(item.payment, IMPORTED_FIELDS))),
                  "user": user.id, "id": item.id}
                 for item in form.changed])

        # Одна запись на прогон, а не на строку: семь тысяч одинаковых строк
        # сделали бы журнал нечитаемым и заслонили бы ручные правки, ради
        # которых он и ведётся.
        handle.execute(
            "INSERT INTO audit_log (user_id, entity, action, changes)"
            " VALUES (%s, 'payment', 'import', %s)",
            (user.id, json.dumps({"новых": len(form.created),
                                  "заменили план": adopted,
                                  "изменено": len(form.changed)})))

    return ImportResult(new=len(form.created), updated=len(form.changed),
                        adopted=adopted)


# Сумма из 1С и пометка о смене суммы. Решает сервер, а не клиент: импорт из
# старой версии приложения иначе молча стёр бы правку человека.
# · в 1С всё та же сумма, что была до правки человека, — правка остаётся;
# · 1С прислала сумму, равную стоящей, — пометка остаётся как была;
# · иначе сумма из 1С, а пометка — «было» то, что стояло, и «кто» не указан
#   (NULL читается как 1С). Если пометку уже ставила выгрузка, «было» остаётся
#   первым, а возврат к нему пометку снимает.
_BY_PERSON = "amount_before IS NOT NULL AND amount_changed_by IS NOT NULL"
_BY_IMPORT = "amount_before IS NOT NULL AND amount_changed_by IS NULL"
_KEEP = f"{_BY_PERSON} AND abs(amount_before - %(amount)s::numeric) < 0.005"
_BACK = f"{_BY_IMPORT} AND abs(amount_before - %(amount)s::numeric) < 0.005"
_SAME = "abs(amount - %(amount)s::numeric) < 0.005"
IMPORTED_AMOUNT = (
    f"amount = CASE WHEN {_KEEP} THEN amount ELSE %(amount)s END,"
    f" amount_before = CASE WHEN {_KEEP} OR {_SAME} THEN amount_before"
    f"   WHEN {_BACK} THEN NULL"
    f"   WHEN {_BY_IMPORT} THEN amount_before ELSE amount END,"
    f" amount_changed_by = CASE WHEN {_KEEP} OR {_SAME} THEN amount_changed_by"
    "   ELSE NULL END,"
    f" amount_changed_at = CASE WHEN {_KEEP} OR {_SAME} THEN amount_changed_at"
    f"   WHEN {_BACK} THEN NULL ELSE now() END"
)


def _row(item: ImportPayment, fields: tuple[str, ...]) -> list[Any]:
    return [getattr(item, name) for name in fields]


class ImportLog(BaseModel):
    path: str = ""
    file_hash: str = ""
    rows_total: int = 0
    rows_new: int = 0
    rows_updated: int = 0
    rows_same: int = 0
    rows_skipped: int = 0
    error: str = ""


@router.post("/log", response_model=ImportRun, summary="Записать прогон в журнал")
def log_import(form: ImportLog,
               user: User = Depends(security.admin_only)) -> ImportRun:
    row = db.fetch_one(
        "INSERT INTO import_run (path, file_hash, rows_total, rows_new,"
        "  rows_updated, rows_same, rows_skipped, error, started_by)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (form.path, form.file_hash, form.rows_total, form.rows_new,
         form.rows_updated, form.rows_same, form.rows_skipped, form.error,
         user.id))
    return ImportRun(**db.fetch_one(_SELECT + " WHERE r.id = %s", (row["id"],)))


@router.get("/before", response_model=dict,
            summary="Когда этот же файл заливали в прошлый раз")
def imported_before(file_hash: str,
                    user: User = Depends(security.current_user)) -> dict:
    if not file_hash:
        return {"finished_at": None}
    row = db.fetch_one(
        "SELECT finished_at FROM import_run"
        " WHERE file_hash = %s AND error = '' ORDER BY id DESC LIMIT 1",
        (file_hash,))
    return {"finished_at": row["finished_at"] if row else None}


@router.get("/last", response_model=ImportRun | None,
            summary="Последний прогон импорта")
def last_import(user: User = Depends(security.current_user)) -> ImportRun | None:
    row = db.fetch_one(_SELECT + " ORDER BY r.finished_at DESC LIMIT 1")
    return ImportRun(**row) if row else None


@router.get("", response_model=list[ImportRun], summary="История импортов")
def list_imports(user: User = Depends(security.current_user),
                 limit: int = 50) -> list[ImportRun]:
    rows = db.fetch_all(
        _SELECT + " ORDER BY r.finished_at DESC LIMIT %s", (min(limit, 500),))
    return [ImportRun(**row) for row in rows]
