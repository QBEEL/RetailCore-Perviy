"""Сценарии модуля оплат: импорт, привязка получателей, пересчёт статусов.

Здесь собрано то, что связывает хранилище оплат с базой поставщиков и с
разбором выгрузки. Интерфейс вызывает только эти функции — они рассчитаны на
работу из фоновой задачи и сообщают о ходе через `progress`.
"""
from __future__ import annotations

from datetime import date
from typing import Callable

from .. import appdata
from ..suppliers import store as suppliers_store
from . import data, importer, transport
from .models import ImportReport, Payment, PaymentOrigin
from .recipients import Guess, guess_supplier, recipient_key

# Данные берутся из действующего источника: общая база, если выполнен вход,
# иначе своя локальная. Имя оставлено прежним — вызовов по нему десяток.
store = data

LOG_FILE = "payments.log"


class ImportNotAllowed(RuntimeError):
    """Импорт в общую базу разрешён только администратору.

    Выгрузка перезаписывает поля во всех оплатах отдела, включая чужие. Это не
    та операция, которую каждый делает у себя, поэтому право на неё выдаётся
    отдельно, а не следует из возможности войти.
    """


def _may_import() -> None:
    if transport.session.active and not transport.session.is_admin:
        raise ImportNotAllowed(
            "Импорт выгрузки 1С в общую базу доступен только администратору.\n\n"
            "Выгрузка обновляет оплаты всего отдела — передайте файл тому, "
            "у кого есть это право.")

Progress = Callable[[int, int], None]


def supplier_names(path: str | None = None) -> dict[int, str]:
    """Карточки поставщиков: номер и имя. Пустая база — не ошибка."""
    try:
        return {s.id: s.name for s in suppliers_store.list_suppliers(path)}
    except Exception:  # noqa: BLE001 — база поставщиков не должна ломать оплаты
        return {}


# --- импорт --------------------------------------------------------------------

def analyze_import(
    path_to_file: str,
    *,
    today: date | None = None,
    progress: Progress | None = None,
    db_path: str | None = None,
) -> ImportReport:
    """Разбирает выгрузку и считает, что даст импорт. В базу ничего не пишет."""
    _may_import()
    existing = store.existing_index(db_path)
    return importer.analyze(path_to_file, existing, today=today, progress=progress)


def apply_import(
    report: ImportReport,
    *,
    today: date | None = None,
    db_path: str | None = None,
    link: bool = True,
) -> ImportReport:
    """Применяет разобранную выгрузку: записывает, привязывает, пересчитывает.

    Индекс существующих записей перечитывается: между разбором и подтверждением
    пользователь мог создать оплату вручную, и затирать её нельзя.
    """
    _may_import()
    existing = store.existing_index(db_path)
    created, changed = importer.split_changes(report, existing)
    written, updated = store.apply_import(created, changed, db_path)
    report.new, report.updated = written, updated
    report.applied = True
    if link:
        auto_link(db_path=db_path)
    store.refresh_overdue(today, db_path)
    store.log_import(
        report.path,
        importer.file_hash(report.path),
        report.rows,
        written,
        updated,
        report.same,
        len(report.skipped),
        path=db_path,
    )
    appdata.log_event(
        LOG_FILE,
        f"Импорт {report.path}\n"
        f"  прочитано {report.rows}, новых {written}, изменено {updated}, "
        f"без изменений {report.same}, пропущено {len(report.skipped)}",
    )
    return report


def already_imported(path_to_file: str, db_path: str | None = None):
    """Когда этот же файл заливали в прошлый раз, если заливали."""
    return store.imported_before(importer.file_hash(path_to_file), db_path)


# --- привязка получателей ------------------------------------------------------

def auto_link(
    *,
    progress: Progress | None = None,
    db_path: str | None = None,
    suppliers_path: str | None = None,
) -> int:
    """Привязывает получателей к карточкам поставщиков там, где имя совпадает.

    Ручные привязки не пересматриваются: пользователь уже решил. Неуверенные
    догадки не применяются — получатель остаётся без карточки, и это нормально,
    оплата считается по текстовому имени.
    """
    names = supplier_names(suppliers_path)
    if not names:
        return 0
    known = store.recipient_links(db_path)
    pending = [name for name, count, total in store.unlinked_recipients(db_path)]
    total = len(pending)
    linked = 0
    for number, recipient in enumerate(pending, start=1):
        if progress is not None and number % 25 == 0:
            progress(number, total)
        if recipient_key(recipient) in known:
            continue
        if (guess := guess_supplier(recipient, names)) is None:
            continue
        store.save_recipient_link(recipient, guess.supplier_id, db_path, manual=False)
        linked += 1
    if progress is not None:
        progress(total, total)
    return linked


def link_candidates(
    *,
    db_path: str | None = None,
    suppliers_path: str | None = None,
    limit: int = 0,
) -> list[tuple[str, int, float, Guess | None]]:
    """Непривязанные получатели с догадками — для ручного разбора в карточке."""
    names = supplier_names(suppliers_path)
    rows = store.unlinked_recipients(db_path)
    if limit:
        rows = rows[:limit]
    return [
        (recipient, count, total, guess_supplier(recipient, names) if names else None)
        for recipient, count, total in rows
    ]


# --- статусы -------------------------------------------------------------------

def refresh(today: date | None = None, db_path: str | None = None) -> int:
    """Пересчитывает просрочку. Вызывается при открытии модуля."""
    return store.refresh_overdue(today, db_path)


def create_from_order(
    recipient: str,
    amount: float,
    *,
    pay_date: date,
    origin: PaymentOrigin = PaymentOrigin.ORDER,
    origin_ref: str = "",
    supplier_id: int = 0,
    responsible: str = "",
    comment: str = "",
) -> Payment:
    """Заготовка оплаты по итогу заказа или переоценки.

    Ничего не сохраняет: карточка открывается заполненной, решение за
    пользователем.
    """
    return Payment(
        amount=amount,
        pay_date=pay_date,
        recipient=recipient,
        supplier_id=supplier_id,
        responsible=responsible or store.current_user(),
        comment=comment,
        origin=origin,
        origin_ref=origin_ref,
    )
