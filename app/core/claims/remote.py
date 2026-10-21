"""Заявки Торг-2 на сервере.

Своей локальной базы у раздела нет, в отличие от оплат: он общий по смыслу —
склад заводит, менеджер отвечает, и без сервера делить друг с другом нечего.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

from .. import appdata
from ..payments import transport
from ..payments.safe_files import safe_name
from ..payments.transport import ServerError
from .models import Brand, Claim, ClaimFile, ClaimHint, Inbox, InboxItem, Manager
from .torg import TorgAct, TorgSettings, file_name

BASE = "/api/claims"
BRANDS = "/api/claim-brands"
TORG = "/api/torg"
CACHE_DIR = "claim_files"


def _ask(method, *args, **kwargs):
    """Запрос с понятным ответом на «раздела там ещё нет».

    Сервер обновляют отдельно от приложения. Пока миграция не применена, все
    адреса раздела отвечают 404, а «Not Found» посреди работы не говорит
    человеку ничего — он начнёт искать причину у себя.
    """
    try:
        return method(*args, **kwargs)
    except ServerError as error:
        if error.status == 404:
            raise ServerError(
                "Общая база ещё не знает про Торг-2: сервер обновлён не до "
                "конца.\n\nПередайте это администратору — нужно применить "
                "миграцию 013_claims.sql и перезапустить API.", 404) from None
        raise


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _moment(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    # Сервер отдаёт время с зоной; приложение показывает местное.
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


def _claim(row: dict) -> Claim:
    return Claim(
        id=int(row["id"]), kind=row["kind"], brand=row["brand"],
        product=row["product"], spec=row["spec"], barcode=row["barcode"],
        quantity=float(row["quantity"]), expiry_date=_date(row.get("expiry_date")),
        # Сервер до кодов маркировки этого поля не присылает.
        mark_code=row.get("mark_code") or "",
        comment=row["comment"], status=row["status"],
        applicant_id=int(row["applicant_id"]), applicant=row["applicant"],
        manager_id=row.get("manager_id"), manager=row.get("manager") or "",
        manager_seen=bool(row.get("manager_seen", True)),
        resolution=row.get("resolution") or "",
        close_note=row.get("close_note") or "",
        created_at=_moment(row.get("created_at")),
        closed_at=_moment(row.get("closed_at")),
        closed_by=row.get("closed_by") or "",
        files=int(row.get("files") or 0),
        # Сервер до актов ТОРГ-2 этих полей не присылает.
        act_id=row.get("act_id"), act_number=row.get("act_number"),
        can_edit=bool(row.get("can_edit")), can_accept=bool(row.get("can_accept")),
        can_close=bool(row.get("can_close")), can_reopen=bool(row.get("can_reopen")),
        can_delete=bool(row.get("can_delete")),
    )


# --- заявки --------------------------------------------------------------------

def list_claims(*, archive: bool = False, mine: bool = False,
                text: str = "") -> list[Claim]:
    rows = _ask(transport.get, BASE, {"archive": archive, "mine": mine, "text": text})
    return [_claim(row) for row in rows]


def create_claim(fields: dict[str, Any]) -> Claim:
    return _claim(_ask(transport.post, BASE, fields))


def update_claim(claim_id: int, fields: dict[str, Any]) -> Claim:
    """Правка: уходят только переданные поля.

    Словарь, а не набор именованных аргументов, чтобы отличить «не менять» от
    «очистить»: срок годности и КМ снимаются передачей `None`.
    """
    return _claim(transport.patch(f"{BASE}/{claim_id}", fields))


def accept_claim(claim_id: int) -> Claim:
    return _claim(transport.post(f"{BASE}/{claim_id}/accept"))


def close_claim(claim_id: int, resolution: str, note: str = "") -> Claim:
    return _claim(transport.post(f"{BASE}/{claim_id}/close",
                                 {"resolution": resolution, "note": note}))


def reopen_claim(claim_id: int) -> Claim:
    return _claim(transport.post(f"{BASE}/{claim_id}/reopen"))


def delete_claim(claim_id: int) -> None:
    transport.delete(f"{BASE}/{claim_id}")


def mark_seen(claim_id: int) -> None:
    """КМ открыл заявку — уведомление о ней больше не нужно."""
    transport.post(f"{BASE}/{claim_id}/seen")


# --- справочное ----------------------------------------------------------------

def managers() -> list[Manager]:
    return [Manager(id=int(row["id"]), full_name=row["full_name"])
            for row in _ask(transport.get, f"{BASE}/managers")]


def inbox() -> Inbox:
    """Что адресовано вошедшему. Опрашивается раз в минуту."""
    data = transport.get(f"{BASE}/inbox")
    return Inbox(
        unseen=int(data.get("unseen", 0)), open=int(data.get("open", 0)),
        items=[InboxItem(id=int(i["id"]), kind=i["kind"], brand=i["brand"],
                         product=i["product"], applicant=i["applicant"])
               for i in data.get("items", [])])


def hint(barcode: str = "", brand: str = "") -> ClaimHint:
    data = transport.get(f"{BASE}/hint", {"barcode": barcode, "brand": brand}) or {}
    return ClaimHint(brand=data.get("brand") or "", product=data.get("product") or "",
                     spec=data.get("spec") or "", manager_id=data.get("manager_id"),
                     marked=bool(data.get("marked")))


# --- вложения ------------------------------------------------------------------

def _file(row: dict) -> ClaimFile:
    return ClaimFile(id=int(row["id"]), claim_id=int(row["claim_id"]),
                     name=row["name"], size=int(row["size"]),
                     added_at=_moment(row["added_at"]))


def list_files(claim_id: int) -> list[ClaimFile]:
    return [_file(row) for row in transport.get(f"{BASE}/{claim_id}/files")]


def attach_file(claim_id: int, source: str) -> ClaimFile:
    if not os.path.isfile(source):
        raise ValueError(f"Файл не найден: {source}")
    return _file(transport.upload(f"{BASE}/{claim_id}/files", source))


def detach_file(file_id: int) -> None:
    transport.delete(f"{BASE}/files/{file_id}")


def local_copy(attachment: ClaimFile) -> str:
    """Скачивает вложение и возвращает путь, по которому его можно открыть.

    Повторное открытие того же файла скачивания не повторяет.
    """
    folder = os.path.join(appdata.path_to(CACHE_DIR), str(attachment.claim_id),
                          str(attachment.id))
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, safe_name(attachment.name, f"file{attachment.id}"))
    if os.path.commonpath([os.path.abspath(target), os.path.abspath(folder)]) \
            != os.path.abspath(folder):
        raise ValueError("Недопустимое имя вложения")
    if os.path.isfile(target) and os.path.getsize(target) == attachment.size:
        return target
    transport.download(f"{BASE}/files/{attachment.id}", target)
    return target


# --- бренды --------------------------------------------------------------------

def brands() -> list[Brand]:
    """Справочник брендов для подсказки при наборе."""
    try:
        rows = transport.get(BRANDS)
    except ServerError as error:
        # Сервер без справочника: заявки заводятся и без подсказки.
        if error.status == 404:
            return []
        raise
    return [Brand(id=int(row["id"]), name=row["name"]) for row in rows]


def add_brand(name: str) -> Brand:
    """Добавляет бренд. Уже существующий сервер отдаёт как есть."""
    row = _ask(transport.post, BRANDS, {"name": name})
    return Brand(id=int(row["id"]), name=row["name"])


def delete_brand(brand_id: int) -> None:
    transport.delete(f"{BRANDS}/{brand_id}")


# --- акты ТОРГ-2 ---------------------------------------------------------------

def torg_settings() -> TorgSettings:
    return TorgSettings.from_dict(_ask(transport.get, f"{TORG}/settings"))


def save_torg_settings(settings: TorgSettings) -> TorgSettings:
    return TorgSettings.from_dict(transport.put(f"{TORG}/settings", settings.to_dict()))


def next_act_number(year: int) -> int:
    return int(transport.get(f"{TORG}/next", {"year": year})["number"])


def torg_suppliers() -> list[dict]:
    """Поставщики из прошлых актов с их договором и доставкой."""
    return list(transport.get(f"{TORG}/suppliers") or [])


def _act(row: dict) -> TorgAct:
    act = TorgAct.from_parts(
        id=int(row["id"]), number=int(row["number"]),
        act_date=_date(row["act_date"]) or date.today(),
        supplier=row["supplier"], payload=row.get("payload") or {})
    return act


def get_act(act_id: int) -> TorgAct:
    return _act(transport.get(f"{TORG}/{act_id}"))


def save_act(act: TorgAct) -> TorgAct:
    """Создаёт акт или правит существующий — по наличию номера записи."""
    body = {"number": act.number, "act_date": act.act_date, "supplier": act.supplier,
            "payload": act.payload(), "claim_ids": act.claim_ids}
    if act.id:
        return _act(transport.put(f"{TORG}/{act.id}", body))
    return _act(_ask(transport.post, TORG, body))


def publish_act_file(act: TorgAct, path: str) -> list[str]:
    """Прикладывает готовый файл акта к каждой его заявке.

    Прежняя версия акта в заявке заменяется: иначе после каждой правки в
    заявке копились бы файлы «ТОРГ-2 №62 …» с разными данными, и непонятно было
    бы, какой из них настоящий. Возвращает тексты неудач — заявка могла
    закрыться, пока акт составлялся, и файл к ней уже не добавить.
    """
    failures: list[str] = []
    name = file_name(act)
    for claim_id in act.claim_ids:
        try:
            for old in list_files(claim_id):
                if old.name == name:
                    detach_file(old.id)
            attach_file(claim_id, path)
        except (ServerError, ValueError, OSError) as failure:
            failures.append(f"Заявка №{claim_id}: {failure}")
    return failures
