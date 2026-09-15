"""Сценарии модуля оплат: импорт, привязка получателей, пересчёт статусов.

Здесь собрано то, что связывает хранилище оплат с базой поставщиков и с
разбором выгрузки. Интерфейс вызывает только эти функции — они рассчитаны на
работу из фоновой задачи и сообщают о ходе через `progress`.
"""
from __future__ import annotations

import calendar
from datetime import date
from typing import Callable, Sequence

from .. import appdata
from ..suppliers import store as suppliers_store
from . import data, importer, plan, remote, sync, transport
# Локальная база под своим именем: `store` в этом модуле — источник по выбору
# пользователя, а выгрузке нужны обе стороны сразу и без подмен.
from . import store as local_store
from .models import ImportReport, Payment, PaymentOrigin
from .recipients import Guess, guess_supplier, recipient_key
from .store import Filter

# Данные берутся из действующего источника: общая база, если выполнен вход,
# иначе своя локальная. Имя оставлено прежним — вызовов по нему десяток.
store = data

LOG_FILE = "payments.log"


class UploadNotAllowed(RuntimeError):
    """Выгрузить свою базу в общую может только администратор.

    Проверяется до чтения обеих баз: отказ после того, как человек дождался
    сравнения семи тысяч строк, — потраченное впустую время.
    """


class PlanNotAllowed(RuntimeError):
    """Загрузить чужой план в общую базу может только администратор.

    Правило то же, что и при создании оплаты вручную: запись заводится на имя
    менеджера, а назначать оплаты на другого человека вправе только админ.
    Проверяется здесь, до разбора файла, — иначе отказ пришёл бы с сервера
    после того, как человек уже дождался разбора и нажал «Применить».
    """


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


# --- план оплат из Excel -------------------------------------------------------

def _may_plan(manager: str) -> None:
    if transport.session.active and not transport.session.may_edit(manager):
        raise PlanNotAllowed(
            f"План закреплён за менеджером «{manager}», а создавать оплаты на "
            "другого человека может только администратор.\n\n"
            "Передайте файл администратору либо укажите в шапке своё имя.")


def manager_names(names: Sequence[str]) -> list[str]:
    """Имена менеджеров для выпадающих списков плана, без повторов.

    Сводятся написания, различающиеся регистром и пробелами: в выгрузке 1С имя
    приходит как его набрали, и «ИВАНОВ  ЕВГЕНИЙ» рядом с «Иванов Евгений» в
    списке выбирать мешает. Остаётся самое частое написание — `known_values`
    отдаёт имена в порядке убывания числа оплат.

    Разные формы одного имени («Дробышева Дарья» и «Дробышева Дарья Сергеевна»)
    это не сводит и не должно: снаружи не видно, один это человек или два, а
    склеить двух менеджеров в одного хуже, чем показать лишнюю строку.
    """
    seen: set[str] = set()
    chosen: list[str] = []
    for name in names:
        key = recipient_key(name)
        if key and key not in seen:
            seen.add(key)
            chosen.append(name)
    return chosen


def plan_template(
    destination: str,
    *,
    manager: str = "",
    year: int = 0,
    month: int = 0,
    db_path: str | None = None,
    suppliers_path: str | None = None,
) -> str:
    """Пустой шаблон плана со списками поставщиков и менеджеров из базы.

    Поставщики берутся из истории оплат, а не только из карточек: менеджер
    планирует тем, кому уже платили, и получателя без карточки в списке быть
    обязан — иначе его впишут руками с новой опечаткой.
    """
    known = store.known_values(db_path)
    names = {name for name in known.get("recipients", []) if name}
    names.update(name for name in supplier_names(suppliers_path).values() if name)
    return plan.write_template(
        destination,
        suppliers=sorted(names, key=str.lower),
        managers=manager_names(known.get("responsible", [])),
        manager=manager,
        year=year,
        month=month,
    )


def analyze_plan(
    path_to_file: str,
    *,
    manager: str = "",
    db_path: str | None = None,
    suppliers_path: str | None = None,
    progress: Progress | None = None,
) -> plan.PlanReport:
    """Разбирает присланный план и считает, что даст загрузка. Ничего не пишет."""
    file = plan.read(path_to_file)
    if manager:
        # Имя из окна сильнее имени из файла: менеджеры копируют шаблон друг у
        # друга и забывают править шапку, а тот, кто грузит, знает, чей файл.
        file.manager = manager
    if not file.manager:
        raise plan.PlanProblem(
            "В файле не указан менеджер. Впишите его в шапку шаблона "
            "(ячейка «Менеджер») или выберите в окне загрузки.")
    _may_plan(file.manager)
    if progress is not None:
        progress(1, 3)
    unlinked = plan.link_suppliers(file, supplier_names(suppliers_path))
    if progress is not None:
        progress(2, 3)
    report = plan.compare(file, plan_payments(file.manager, file.months, db_path))
    report.unlinked = unlinked
    if progress is not None:
        progress(3, 3)
    return report


def plan_payments(
    manager: str,
    months: list[tuple[int, int]],
    db_path: str | None = None,
) -> list[Payment]:
    """Прошлый план менеджера за эти месяцы — то, что заменит новый файл."""
    if not months:
        return []
    first_year, first_month = months[0]
    last_year, last_month = months[-1]
    selection = Filter(
        start=date(first_year, first_month, 1),
        end=date(last_year, last_month, calendar.monthrange(last_year, last_month)[1]),
    )
    return plan.existing_of(store.list_payments(selection, db_path), manager, months)


def apply_plan(
    report: plan.PlanReport,
    *,
    db_path: str | None = None,
    today: date | None = None,
    progress: Progress | None = None,
) -> plan.PlanReport:
    """Применяет разобранный план: создаёт, правит и убирает лишнее.

    Порядок важен: сначала записывается новое и правится изменившееся, и лишь
    потом удаляется исчезнувшее. При обрыве связи на середине в базе останется
    лишняя оплата, а не пропавшая, — заметить лишнюю проще.
    """
    _may_plan(report.plan.manager)
    done, total = 0, report.changes
    for payment in [*report.created, *report.updated]:
        store.save_payment(payment, db_path)
        done += 1
        if progress is not None:
            progress(done, total)
    removed = 0
    for payment in report.removed:
        if store.delete_payment(payment.id, db_path):
            removed += 1
        done += 1
        if progress is not None:
            progress(done, total)
    report.applied = True
    store.refresh_overdue(today, db_path)
    appdata.log_event(
        LOG_FILE,
        f"План {report.plan.path}\n"
        f"  менеджер {report.plan.manager}, {report.plan.months_title}\n"
        f"  строк {len(report.plan.rows)}, новых {len(report.created)}, "
        f"изменено {len(report.updated)}, удалено {removed}, "
        f"без изменений {len(report.same)}, оплачено {len(report.paid)}, "
        f"пропущено {len(report.plan.skipped)}",
    )
    return report


# --- выгрузка локальной базы в общую -------------------------------------------

# По сколько строк уходит за раз. Полная история — около семи тысяч, и одним
# запросом это несколько мегабайт; сервер каждую часть пишет транзакцией.
# Оборванная на середине выгрузка не портит базу: повторная догоняет остаток,
# потому что уже записанное сравнение опознаёт как совпавшее.
BATCH = 500


def _may_upload() -> None:
    if not transport.session.active:
        raise UploadNotAllowed(
            "Выгрузка идёт в общую базу, а вход не выполнен.")
    if not transport.session.is_admin:
        raise UploadNotAllowed(
            "Выгрузка своей базы в общую доступна только администратору.\n\n"
            "Она затрагивает оплаты всего отдела, включая чужие.")


def analyze_upload(
    *,
    db_path: str | None = None,
    progress: Progress | None = None,
) -> sync.SyncReport:
    """Сравнивает локальную базу с общей. Ни в одну ничего не пишет."""
    _may_upload()
    mine = local_store.list_payments(None, db_path)
    theirs = remote.list_payments()
    report = sync.compare(
        mine, theirs,
        local_budgets=local_store.budgets(db_path),
        remote_budgets=remote.budgets(),
        progress=progress)
    return report


def apply_upload(
    report: sync.SyncReport,
    *,
    progress: Progress | None = None,
) -> sync.SyncReport:
    """Отправляет разобранную выгрузку на сервер частями."""
    _may_upload()
    done, total = 0, report.changes
    written = 0
    for part in sync.batches(report.created, BATCH):
        answer = remote.upload([sync.pack(payment) for payment in part], [])
        written += answer["new"]
        done += len(part)
        if progress is not None:
            progress(done, total)
    for part in sync.batches(report.updated, BATCH):
        answer = remote.upload([], [
            {"id": change.remote.id, "payment": sync.pack(change.local)}
            for change in part])
        written += answer["updated"]
        done += len(part)
        if progress is not None:
            progress(done, total)
    if report.budgets:
        remote.upload([], [], [
            {"year": budget.year, "month": budget.month,
             "amount": budget.amount, "note": budget.note}
            for budget in report.budgets])
        done += len(report.budgets)
        if progress is not None:
            progress(done, total)
    report.written = written
    report.applied = True
    appdata.log_event(
        LOG_FILE,
        f"Выгрузка локальной базы в общую\n"
        f"  новых {len(report.created)}, обновлено {len(report.updated)}, "
        f"бюджетов {len(report.budgets)}, совпало {len(report.same)}, "
        f"только на сервере {report.only_remote}",
    )
    return report


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
