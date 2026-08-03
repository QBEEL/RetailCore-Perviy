"""Оплаты с сервера. Замена `store` для работы с общей базой.

Имена функций, аргументы и типы возвращаемых значений повторяют `store`, чтобы
интерфейс не заметил подмены: таблица, календарь и аналитика продолжают
получать те же `Payment`, `Budget` и `PaymentFile`.

Отличие одно и оно принципиальное: у платежа появилось право на правку. База
общая, и «изменить» больше не значит «можно». Признак приходит с сервера
готовым (`editable`) — повторять правило на клиенте нельзя, иначе однажды оно
разойдётся с серверным, и пользователь увидит доступную кнопку, которая не
работает.

Аргумент `path` во всех функциях сохранён и не используется: интерфейс передаёт
его позиционно во множестве мест, и убирать его пришлось бы вместе с правкой
всех вызовов.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Sequence

from .. import appdata
from . import transport
from .models import (
    Budget,
    Payment,
    PaymentFile,
    PaymentOrigin,
    PaymentStatus,
    SupplierRow,
)
from .recipients import recipient_key
from .store import Filter

# Куда складываются скачанные вложения. Папка временная по смыслу: файл всегда
# можно скачать заново, а чистится она вместе с профилем.
CACHE_DIR = "payment_cache"


# --- разбор ответов ------------------------------------------------------------

def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _moment(value: Any) -> datetime | None:
    try:
        # Сервер отдаёт время с зоной; приложение показывает местное и о зонах
        # ничего не знает, поэтому она отбрасывается после перевода.
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


def _status(value: Any) -> PaymentStatus:
    try:
        return PaymentStatus(str(value))
    except ValueError:
        return PaymentStatus.PLANNED


def _origin(value: Any) -> PaymentOrigin:
    try:
        return PaymentOrigin(str(value))
    except ValueError:
        return PaymentOrigin.MANUAL


def _payment(row: dict) -> Payment:
    payment = Payment(
        id=int(row["id"]),
        doc_number=row["doc_number"],
        request_date=_date(row["request_date"]),
        pay_date=_date(row["pay_date"]),
        amount=float(row["amount"]),
        vat=float(row["vat"]),
        currency=row["currency"],
        supplier_id=int(row["supplier_id"]),
        recipient=row["recipient"],
        status=_status(row["status"]),
        source_status=row["source_status"],
        paid_flag=bool(row["paid_flag"]),
        operation=row["operation"],
        over_limit=bool(row["over_limit"]),
        priority=row["priority"],
        edo_state=row["edo_state"],
        responsible=row["responsible"],
        author=row["author"],
        comment=row["comment"],
        had_files=bool(row["had_files"]),
        origin=_origin(row["origin"]),
        origin_ref=row["origin_ref"],
        created_at=_moment(row["created_at"]),
        updated_at=_moment(row["updated_at"]),
        files=int(row.get("files", 0)),
    )
    # Право на правку не входит в dataclass: он общий с локальным режимом.
    # Хранится рядом, читается через may_edit().
    _remember(payment.id, bool(row.get("editable", False)))
    return payment


# Права по номеру платежа: сервер присылает их с каждой записью, здесь они
# копятся.
#
# Словарь намеренно не очищается при каждом отборе. Карточка оплаты, открываясь,
# подгружает историю по получателю — это тоже отбор, и сброс на нём стирал
# право на саму открытую запись: своя оплата открывалась только на просмотр.
# Накопленные права сбрасываются вместе со сменой пользователя, не раньше.
_RIGHTS: dict[int, bool] = {}
_RIGHTS_OWNER: str = ""


def _remember(payment_id: int, editable: bool) -> None:
    global _RIGHTS_OWNER

    if _RIGHTS_OWNER != transport.session.login:
        _RIGHTS.clear()
        _RIGHTS_OWNER = transport.session.login
    _RIGHTS[payment_id] = editable


def may_edit(payment: Payment | int) -> bool:
    """Вправе ли текущий пользователь править эту оплату.

    Решение принимает сервер и присылает готовым в поле `editable`; здесь оно
    только запоминается. Повторять правило на клиенте нельзя — однажды копия
    разойдётся с оригиналом, и человек увидит доступную кнопку, которая не
    работает.
    """
    payment_id = payment if isinstance(payment, int) else payment.id
    if not payment_id:
        # Новая запись: создать её можно всегда, ответственный подставится свой.
        return True
    if _RIGHTS_OWNER != transport.session.login:
        return False
    return _RIGHTS.get(payment_id, False)


def _budget(row: dict) -> Budget:
    return Budget(year=int(row["year"]), month=int(row["month"]),
                  amount=float(row["amount"]), note=row["note"],
                  updated_at=_moment(row["updated_at"]))


def _file(row: dict) -> PaymentFile:
    return PaymentFile(id=int(row["id"]), payment_id=int(row["payment_id"]),
                       name=row["name"], path="", size=int(row["size"]),
                       added_at=_moment(row["added_at"]))


# --- оплаты --------------------------------------------------------------------

def _params(selection: Filter | None) -> dict[str, Any]:
    """Условия отбора → параметры запроса. Пустые поля не передаются."""
    chosen = selection or Filter()
    return {
        "text": chosen.text,
        "start": chosen.start,
        "end": chosen.end,
        "statuses": [status.value for status in chosen.statuses],
        "supplier_id": chosen.supplier_id or None,
        # Отбор идёт по нормализованному ключу, а не по имени: «НеваЛайн ООО»
        # и «ООО "Невалайн"» — один получатель. Ключ считается той же
        # функцией, что и при записи, — двух реализаций у него быть не должно.
        "recipient_key": recipient_key(chosen.recipient) if chosen.recipient else None,
        "amount_from": chosen.amount_from,
        "amount_to": chosen.amount_to,
        "responsible": chosen.responsible,
        "operation": chosen.operation,
        "over_limit": chosen.over_limit,
        "dated_only": chosen.dated_only or None,
    }


def list_payments(
    selection: Filter | None = None,
    path: str | None = None,
    *,
    order: str = "",
    limit: int = 0,
) -> list[Payment]:
    """Отбор оплат. Порядок задаёт сервер, аргумент `order` не используется."""
    rows = transport.get("/api/payments", _params(selection))
    payments = [_payment(row) for row in rows]
    return payments[:limit] if limit else payments


def get_payment(payment_id: int, path: str | None = None) -> Payment | None:
    try:
        return _payment(transport.get(f"/api/payments/{payment_id}"))
    except transport.ServerError as error:
        if error.status == 404:
            return None
        raise


def save_payment(payment: Payment, path: str | None = None) -> Payment:
    """Создаёт или обновляет платёж.

    Проверки повторяют локальные: отказать до обращения к серверу дешевле и
    понятнее, чем показать пользователю ответ об ошибке проверки полей.
    """
    if payment.amount <= 0:
        raise ValueError("Сумма оплаты должна быть больше нуля")
    if not (payment.recipient.strip() or payment.supplier_id):
        raise ValueError("У оплаты должен быть получатель")

    if payment.id:
        row = transport.patch(f"/api/payments/{payment.id}", {
            "pay_date": payment.pay_date,
            "clear_pay_date": payment.pay_date is None,
            "status": payment.status.value,
            "comment": payment.comment,
            "supplier_id": payment.supplier_id,
            "amount": payment.amount,
            "priority": payment.priority,
        })
    else:
        row = transport.post("/api/payments", {
            "pay_date": payment.pay_date,
            "amount": payment.amount,
            "vat": payment.vat,
            "currency": payment.currency,
            "supplier_id": payment.supplier_id,
            "recipient": payment.recipient,
            "status": payment.status.value,
            "comment": payment.comment,
            "responsible": payment.responsible,
            "operation": payment.operation,
            "priority": payment.priority,
        })
    return _payment(row)


def delete_payment(payment_id: int, path: str | None = None) -> bool:
    try:
        transport.delete(f"/api/payments/{payment_id}")
        return True
    except transport.ServerError as error:
        if error.status == 404:
            return False
        raise


def update_many(
    ids: Sequence[int],
    path: str | None = None,
    *,
    status: PaymentStatus | None = None,
    pay_date: date | None = None,
    responsible: str | None = None,
    supplier_id: int | None = None,
) -> int:
    """Массовое изменение. Возвращает число действительно изменённых строк.

    Чужие оплаты сервер отклоняет молча и перечисляет их отдельно: остановить
    всю операцию из-за одной чужой строки в выделении было бы хуже — человек
    выделил сотню и не обязан помнить, какие из них его.
    """
    if not ids:
        return 0
    patch: dict[str, Any] = {}
    if status is not None:
        patch["status"] = status.value
    if pay_date is not None:
        patch["pay_date"] = pay_date
    if supplier_id is not None:
        patch["supplier_id"] = supplier_id
    if responsible is not None:
        # Ответственный правится только через учётные записи: сменить его в
        # оплате значило бы отдать запись другому человеку в обход прав.
        raise ValueError(
            "Сменить ответственного можно только в настройках учётной записи")
    if not patch:
        return 0
    answer = transport.patch("/api/payments",
                             {"ids": list(ids), "patch": patch})
    return int(answer.get("changed", 0))


def refresh_overdue(today: date | None = None, path: str | None = None) -> int:
    answer = transport.post("/api/payments/refresh-overdue")
    return int(answer.get("changed", 0))


def known_values(path: str | None = None) -> dict[str, list[str]]:
    return transport.get("/api/payments/known")


def count_payments(path: str | None = None) -> int:
    return int(transport.get("/api/payments/count").get("total", 0))


def last_import(path: str | None = None) -> dict[str, Any] | None:
    return transport.get("/api/imports/last")


# --- импорт --------------------------------------------------------------------

# Поля, приходящие из выгрузки 1С. Держатся в одном виде с IMPORTED_FIELDS
# локального хранилища — по ним сравнивается «изменилось ли».
_FLAGS = ("paid_flag", "over_limit", "had_files")


def _comparable(values: dict[str, Any]) -> dict[str, Any]:
    """Значения с сервера → тот же вид, что отдаёт локальная база.

    Сравнение в `importer._differs` идёт через `str(before) != str(after)`, и
    типы обязаны совпадать буквально. Сервер присылает `true` и `null`, база
    отдаёт `1` и пустую строку: без приведения каждая из семи тысяч строк
    выглядела бы изменившейся, и импорт переписывал бы базу целиком на каждом
    прогоне.
    """
    ready = dict(values)
    for name in _FLAGS:
        if name in ready:
            ready[name] = int(bool(ready[name]))
    if "pay_date" in ready:
        ready["pay_date"] = ready["pay_date"] or ""
    for name in ("amount", "vat"):
        if name in ready:
            ready[name] = float(ready[name] or 0.0)
    return ready


def existing_index(path: str | None = None) -> dict[tuple[str, str], Any]:
    """Записи из прошлых выгрузок по ключу «номер + дата заявки»."""
    from .store import Existing

    index: dict[tuple[str, str], Existing] = {}
    for row in transport.get("/api/imports/index"):
        key = (row["doc_number"], row["request_date"] or "")
        index[key] = Existing(
            id=int(row["id"]),
            origin=row["origin"],
            values=_comparable(row["values"]),
            status=row["status"],
            manual=bool(row["manual"]),
        )
    return index


def _for_server(payment: Payment) -> dict[str, Any]:
    """Платёж → тело запроса. Даты уходят строкой ISO, флаги — логическими."""
    return {
        "doc_number": payment.doc_number,
        "request_date": payment.request_date,
        "pay_date": payment.pay_date,
        "amount": float(payment.amount),
        "vat": float(payment.vat),
        "currency": payment.currency,
        "supplier_id": int(payment.supplier_id),
        "recipient": payment.recipient.strip(),
        "recipient_key": recipient_key(payment.recipient),
        "status": payment.status.value,
        "source_status": payment.source_status,
        "paid_flag": bool(payment.paid_flag),
        "operation": payment.operation,
        "over_limit": bool(payment.over_limit),
        "priority": payment.priority,
        "edo_state": payment.edo_state,
        "responsible": payment.responsible,
        "author": payment.author,
        "comment": payment.comment,
        "had_files": bool(payment.had_files),
        "origin": payment.origin.value,
        "origin_ref": payment.origin_ref,
    }


def apply_import(created: Any, changed: Any,
                 path: str | None = None) -> tuple[int, int]:
    """Отправляет разобранную выгрузку. Сервер пишет её одной транзакцией."""
    new_rows = [_for_server(payment) for payment in created]
    updates = [{"id": int(payment_id), "payment": _for_server(payment)}
               for payment_id, payment in changed]
    if not new_rows and not updates:
        return 0, 0
    answer = transport.post("/api/imports/apply",
                            {"created": new_rows, "changed": updates})
    return int(answer["new"]), int(answer["updated"])


def log_import(path_to_file: str, file_hash: str, rows: int, created: int,
               changed: int, same: int, skipped: int, error: str = "",
               path: str | None = None) -> None:
    transport.post("/api/imports/log", {
        "path": os.path.basename(path_to_file), "file_hash": file_hash,
        "rows_total": rows, "rows_new": created, "rows_updated": changed,
        "rows_same": same, "rows_skipped": skipped, "error": error})


def imported_before(file_hash: str, path: str | None = None) -> datetime | None:
    if not file_hash:
        return None
    answer = transport.get("/api/imports/before", {"file_hash": file_hash})
    return _moment(answer.get("finished_at"))


# --- бюджеты -------------------------------------------------------------------

def budgets(path: str | None = None) -> list[Budget]:
    return [_budget(row) for row in transport.get("/api/budgets")]


def get_budget(year: int, month: int, path: str | None = None) -> Budget | None:
    for budget in budgets():
        if budget.year == year and budget.month == month:
            return budget
    return None


def save_budget(budget: Budget, path: str | None = None) -> Budget:
    return _budget(transport.put("/api/budgets", {
        "year": budget.year, "month": budget.month,
        "amount": budget.amount, "note": budget.note}))


def delete_budget(year: int, month: int, path: str | None = None) -> bool:
    try:
        transport.delete(f"/api/budgets/{year}/{month}")
        return True
    except transport.ServerError as error:
        if error.status == 404:
            return False
        raise


# --- получатели ----------------------------------------------------------------

def recipient_links(path: str | None = None) -> dict[str, int]:
    return {row["recipient_key"]: int(row["supplier_id"])
            for row in transport.get("/api/recipients/links")}


def save_recipient_link(recipient: str, supplier_id: int,
                        path: str | None = None, *, manual: bool = True) -> int:
    if not recipient.strip():
        raise ValueError("Получателя нечем опознать")
    transport.put("/api/recipients/links",
                  {"recipient": recipient, "supplier_id": supplier_id})
    return 1


def drop_recipient_link(recipient: str, path: str | None = None) -> bool:
    from .recipients import recipient_key

    key = recipient_key(recipient)
    try:
        transport.delete(f"/api/recipients/links/{key}")
        return True
    except transport.ServerError as error:
        if error.status == 404:
            return False
        raise


def suppliers(responsible: str = "", months: int = 0,
              path: str | None = None) -> list[SupplierRow]:
    """Поставщики из оплат с их менеджерами. Считает сервер."""
    rows = transport.get("/api/payments/suppliers",
                         {"responsible": responsible, "months": months or None})
    return [
        SupplierRow(
            recipient_key=row["recipient_key"], recipient=row["recipient"],
            supplier_id=int(row["supplier_id"] or 0),
            payments=int(row["payments"]), amount=float(row["amount"] or 0.0),
            last_pay=_date(row["last_pay"]), managers=list(row["managers"]),
        )
        for row in rows
    ]


def unlinked_recipients(path: str | None = None) -> list[tuple[str, int, float]]:
    return [(row["recipient"], int(row["payments"]), float(row["amount"]))
            for row in transport.get("/api/recipients/unlinked")]


# --- вложения ------------------------------------------------------------------

def files(payment_id: int, path: str | None = None) -> list[PaymentFile]:
    return [_file(row)
            for row in transport.get(f"/api/payments/{payment_id}/files")]


def attach_file(payment_id: int, source: str, path: str | None = None) -> PaymentFile:
    if not os.path.isfile(source):
        raise ValueError(f"Файл не найден: {source}")
    return _file(transport.upload(f"/api/payments/{payment_id}/files", source))


def detach_file(file_id: int, path: str | None = None) -> bool:
    try:
        transport.delete(f"/api/payments/files/{file_id}")
        return True
    except transport.ServerError as error:
        if error.status == 404:
            return False
        raise


def file_available(attachment: PaymentFile) -> bool:
    """Вложение лежит на сервере: раз оно есть в списке, оно доступно."""
    return True


def open_path(attachment: PaymentFile) -> str:
    """Путь для открытия. Файл при необходимости скачивается."""
    return local_copy(attachment)


def local_copy(attachment: PaymentFile) -> str:
    """Скачивает вложение и возвращает путь, по которому его можно открыть.

    Файл лежит на сервере, а `os.startfile` умеет открывать только локальный
    путь. Повторное открытие того же вложения скачивания не повторяет.
    """
    folder = os.path.join(appdata.path_to(CACHE_DIR), str(attachment.payment_id))
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, attachment.name)
    if os.path.isfile(target) and os.path.getsize(target) == attachment.size:
        return target
    transport.download(f"/api/payments/files/{attachment.id}", target)
    return target


# --- прочее --------------------------------------------------------------------

def current_user() -> str:
    """Имя вошедшего — им подписываются созданные записи."""
    return transport.session.full_name or transport.session.login


def database_size(path: str | None = None) -> int:
    """База на сервере: её размер приложению неизвестен и не нужен."""
    return 0
