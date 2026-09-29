"""Импорт выгрузки оплат из 1С.

Файл приходит в cp1251 с разделителем «;», суммы записаны как «1 649 018,00».
Три особенности выгрузки требуют отдельного обращения.

Номер заявки обнуляется каждый год: `IP00-000001` встречается и в 2021-м, и в
2026-м, каждый раз с другим поставщиком. Одного номера для опознания записи
мало, ключом служит пара с датой заявки — на 6929 строках она не дала ни одной
коллизии.

Для заявок, созданных в день выгрузки, 1С печатает вместо даты только время
(«11:19»). Такая ячейка читается как дата файла.

Статус собирается из двух колонок: согласования и отметки об оплате. Факт
оплаты сильнее согласования — в выгрузке есть три заявки, помеченные
оплаченными, но так и не согласованные.

Разбор ничего не пишет: он возвращает отчёт, который показывается пользователю,
и только подтверждённый отчёт применяется к базе.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
from datetime import date, datetime
from typing import Callable, Iterable, Sequence

from ..normalize import normalize_text
from .models import (
    AMOUNT_EPSILON,
    ImportReport,
    Payment,
    PaymentOrigin,
    PaymentStatus,
    SUPPLIER_OPERATION,
)
from .recipients import clean_name, recipient_key

# Кодировки в порядке проверки: 1С выгружает в cp1251, но файл могли
# переоткрыть и сохранить в Excel.
ENCODINGS: tuple[str, ...] = ("utf-8-sig", "cp1251", "utf-8")
DELIMITERS: tuple[str, ...] = (";", "\t", ",")

# Заголовки колонок выгрузки. Ключ — нормализованное название, значение — поле.
# Сопоставление по названию, а не по номеру: порядок колонок в 1С настраивается
# пользователем и меняется между выгрузками.
COLUMNS: dict[str, str] = {
    "номер": "doc_number",
    "дата заявки": "request_date",
    "есть файлы": "had_files",
    "сумма": "amount",
    "ндс": "vat",
    "валюта": "currency",
    "статус": "source_status",
    "сверх лимита": "over_limit",
    "приоритет": "priority",
    "дата платежа": "pay_date",
    "оплачена закрыта": "paid_flag",
    "хозяйственная операция": "operation",
    "получатель": "recipient",
    "состояние эдо": "edo_state",
    "заявитель": "responsible",
    "автор": "author",
}

# Без этих колонок файл не выгрузка оплат, а что-то другое.
REQUIRED: tuple[str, ...] = ("doc_number", "request_date", "amount", "recipient")

_DATE_RE = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_YES = frozenset({"да", "yes", "1", "истина", "true"})
_REJECTED = "отклонена"


class ImportProblem(ValueError):
    """Файл нельзя прочитать как выгрузку оплат."""


def file_hash(path: str) -> str:
    """Отпечаток файла — чтобы узнать уже залитую выгрузку."""
    digest = hashlib.sha1()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def read_text(path: str) -> str:
    """Читает файл, подбирая кодировку."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as error:
        raise ImportProblem(f"Файл не открывается: {error}") from error
    if not raw.strip():
        raise ImportProblem("Файл пустой")
    for encoding in ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        # Признак неверной кодировки: кириллица рассыпалась в замены.
        if text.count("�") > len(text) // 100:
            continue
        return text
    raise ImportProblem(
        "Не удалось определить кодировку файла. Сохраните выгрузку в UTF-8 или Windows-1251.")


def sniff_delimiter(text: str) -> str:
    """Разделитель — тот, что чаще встречается в строке заголовков."""
    head = text.splitlines()[0] if text else ""
    counts = {delimiter: head.count(delimiter) for delimiter in DELIMITERS}
    best = max(counts, key=lambda key: counts[key])
    return best if counts[best] else ";"


def read_rows(path: str) -> tuple[list[str], list[list[str]]]:
    """Заголовки и строки файла."""
    text = read_text(path)
    delimiter = sniff_delimiter(text)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if len(rows) < 2:
        raise ImportProblem("В файле нет строк с данными")
    return rows[0], rows[1:]


def map_columns(header: Sequence[str]) -> dict[str, int]:
    """Номера колонок по их назначению."""
    found: dict[str, int] = {}
    for index, title in enumerate(header):
        if field := COLUMNS.get(normalize_text(title)):
            found.setdefault(field, index)
    missing = [name for name in REQUIRED if name not in found]
    if missing:
        titles = {value: key for key, value in COLUMNS.items()}
        names = ", ".join(f"«{titles[name]}»" for name in missing)
        raise ImportProblem(
            f"В файле не найдены обязательные колонки: {names}. "
            "Похоже, это не выгрузка «Оплата поставщикам».")
    return found


def parse_amount(value: object) -> float:
    """«1 649 018,00», «55 410,43», «500» → число. Пробелы бывают неразрывными."""
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    for space in (" ", " ", " ", " ", " ", "'"):
        text = text.replace(space, "")
    text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_date(value: object, fallback: date | None = None) -> date | None:
    """«25.10.2022» → дата. Одно время без даты означает день выгрузки."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if match := _DATE_RE.match(text):
        day, month, year = (int(part) for part in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    if _TIME_RE.match(text):
        # 1С печатает только время для заявок, созданных в день выгрузки.
        return fallback
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _flag(value: object) -> bool:
    return normalize_text(value) in _YES


def export_date(path: str) -> date:
    """День выгрузки — по времени изменения файла."""
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).date()
    except OSError:
        return date.today()


def status_of(
    paid: bool,
    source_status: str,
    pay_date: date | None,
    today: date,
) -> PaymentStatus:
    """Статус приложения по паре колонок 1С.

    Факт оплаты сильнее согласования: в выгрузке есть заявки, помеченные
    оплаченными при статусе «Не согласована», и деньги по ним уже ушли.
    """
    if paid:
        return PaymentStatus.PAID
    if normalize_text(source_status) == _REJECTED:
        return PaymentStatus.CANCELLED
    if pay_date is not None and pay_date < today:
        return PaymentStatus.OVERDUE
    return PaymentStatus.PLANNED


def parse(
    path: str,
    *,
    today: date | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[Payment], list[str]]:
    """Разбирает файл в платежи. Возвращает их и список пропущенных строк."""
    header, rows = read_rows(path)
    columns = map_columns(header)
    moment = today or date.today()
    fallback = export_date(path)
    total = len(rows)
    payments: list[Payment] = []
    skipped: list[str] = []

    def cell(row: Sequence[str], name: str) -> str:
        index = columns.get(name, -1)
        return row[index].strip() if 0 <= index < len(row) else ""

    for number, row in enumerate(rows, start=2):
        if progress is not None and number % 500 == 0:
            progress(number, total)
        doc_number = cell(row, "doc_number")
        request_date = parse_date(cell(row, "request_date"), fallback)
        amount = parse_amount(cell(row, "amount"))
        recipient = clean_name(cell(row, "recipient"))
        if not doc_number:
            skipped.append(f"строка {number}: нет номера заявки")
            continue
        if request_date is None:
            skipped.append(f"строка {number}: не разобрана дата заявки «{cell(row, 'request_date')}»")
            continue
        if amount <= 0:
            skipped.append(f"строка {number}: сумма не разобрана или равна нулю")
            continue
        pay_date = parse_date(cell(row, "pay_date"), fallback)
        source_status = cell(row, "source_status")
        paid = _flag(cell(row, "paid_flag"))
        payments.append(Payment(
            doc_number=doc_number,
            request_date=request_date,
            pay_date=pay_date,
            amount=amount,
            vat=parse_amount(cell(row, "vat")),
            currency=cell(row, "currency") or "руб.",
            recipient=recipient,
            status=status_of(paid, source_status, pay_date, moment),
            source_status=source_status,
            paid_flag=paid,
            operation=cell(row, "operation") or SUPPLIER_OPERATION,
            over_limit=_flag(cell(row, "over_limit")),
            priority=cell(row, "priority"),
            edo_state=cell(row, "edo_state"),
            responsible=clean_name(cell(row, "responsible")),
            author=clean_name(cell(row, "author")),
            had_files=_flag(cell(row, "had_files")),
            origin=PaymentOrigin.IMPORT,
        ))
    if progress is not None:
        progress(total, total)
    if not payments:
        raise ImportProblem(
            "Ни одна строка не разобрана. Проверьте, что это выгрузка «Оплата поставщикам».")
    return payments, skipped


def analyze(
    path: str,
    existing: dict[tuple[str, str], object],
    *,
    candidates: Sequence[Payment] = (),
    today: date | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> ImportReport:
    """Считает, что даст импорт, ничего не записывая.

    Сравнение идёт по полям, пришедшим из 1С. Комментарий, вложения и статус,
    выставленный человеком, в сравнении не участвуют — их импорт не меняет.
    """
    payments, skipped = parse(path, today=today, progress=progress)
    report = ImportReport(path=path, rows=len(payments) + len(skipped), skipped=skipped)
    seen: set[tuple[str, str]] = set()
    fresh: list[Payment] = []
    for payment in payments:
        key = payment.key
        if key in seen:
            # Внутри одного файла пара «номер + дата» уникальна, но выгрузку
            # могли склеить из двух — вторую копию берём как изменение первой.
            report.same += 1
            continue
        seen.add(key)
        found = existing.get(key)
        if found is None:
            fresh.append(payment)
        elif _differs(payment, found):
            report.updated += 1
        else:
            report.same += 1
        report.payments.append(payment)
    report.adopted = len(adoptions(fresh, candidates))
    report.new = len(fresh) - report.adopted
    dates = [p.pay_date for p in report.payments if p.pay_date]
    report.first_pay = min(dates) if dates else None
    report.last_pay = max(dates) if dates else None
    report.recipients = len({recipient_key(p.recipient) for p in report.payments if p.recipient})
    return report


def _differs(payment: Payment, existing: object) -> bool:
    """Отличается ли запись от лежащей в базе по полям из 1С."""
    from .store import IMPORTED_FIELDS, imported_values

    values = imported_values(payment)
    stored = getattr(existing, "values", {})
    for name in IMPORTED_FIELDS:
        before, after = stored.get(name), values.get(name)
        if isinstance(after, float) or isinstance(before, float):
            if abs(float(before or 0.0) - float(after or 0.0)) > 0.005:
                return True
        elif str(before or "") != str(after or ""):
            return True
    return False


def _keep_manual_amount(payment: Payment, found: object) -> None:
    """Сумма, поправленная человеком, переживает импорт — пока 1С её не сменила.

    В 1С всё та же сумма, что была до правки, — значит, правка новее выгрузки,
    и перезаписать её означало бы молча откатить решение человека каждым
    понедельничным импортом. Пришла другая сумма — она новее правки и
    берётся как есть. Сервер применяет то же правило сам; здесь оно нужно,
    чтобы предпросмотр не обещал изменить то, что не изменится.
    """
    before = getattr(found, "amount_before", None)
    if before is None or abs(float(before) - payment.amount) >= AMOUNT_EPSILON:
        return
    stored = getattr(found, "values", {}).get("amount")
    if stored is not None:
        payment.amount = float(stored)


def adoptions(
    fresh: Sequence[Payment],
    candidates: Sequence[Payment],
) -> dict[int, Payment]:
    """Какие новые заявки 1С встают на место заведённых в приложении оплат.

    Оплату сначала намечают в приложении — вручную или планом менеджера, — а
    через день-другой по ней же заводят заявку в 1С. Без сопоставления импорт
    клал бы заявку рядом, и бюджет дня считал бы одни деньги дважды.

    Своя запись узнаётся по получателю и дате платежа. Сумма в условие не
    входит: в плане она обычно круглая («800 000»), а в заявке — по счёту
    («855 876»). Если на день у получателя несколько открытых записей, заявке
    достаётся ближайшая по сумме. Заявка без пары остаётся новой: сумма из 1С
    всё равно главнее, и терять её нельзя.

    Возвращает номер записи в базе → заявку, которая её заменит.
    """
    buckets: dict[tuple[str, date], list[Payment]] = {}
    for candidate in candidates:
        if candidate.doc_number or candidate.pay_date is None or not candidate.status.open:
            continue
        buckets.setdefault(
            (recipient_key(candidate.recipient), candidate.pay_date), []).append(candidate)
    found: dict[int, Payment] = {}
    for payment in fresh:
        if payment.pay_date is None:
            continue
        group = buckets.get((recipient_key(payment.recipient), payment.pay_date))
        if not group:
            continue
        nearest = min(group, key=lambda c: (abs(c.amount - payment.amount), c.id))
        group.remove(nearest)
        found[nearest.id] = payment
    return found


def split_changes(
    report: ImportReport,
    existing: dict[tuple[str, str], object],
    candidates: Sequence[Payment] = (),
) -> tuple[list[Payment], list[tuple[int, Payment]], list[tuple[int, Payment]]]:
    """Делит разобранное на создаваемое, обновляемое и занимающее своё место.

    Записи, созданные в приложении, по номеру заявки импорт не ищет: у них нет
    пары «номер + дата заявки» из выгрузки. Но новая заявка на того же
    получателя и тот же день забирает открытую ручную или плановую запись
    (`adoptions`) — иначе одна оплата жила бы в базе дважды.
    """
    created: list[Payment] = []
    changed: list[tuple[int, Payment]] = []
    seen: set[tuple[str, str]] = set()
    for payment in report.payments:
        key = payment.key
        if key in seen:
            continue
        seen.add(key)
        found = existing.get(key)
        if found is None:
            created.append(payment)
            continue
        if getattr(found, "origin", "") != PaymentOrigin.IMPORT.value:
            continue
        _keep_manual_amount(payment, found)
        if _differs(payment, found):
            changed.append((int(getattr(found, "id", 0)), payment))
    adopted = adoptions(created, candidates)
    taken = {id(payment) for payment in adopted.values()}
    created = [payment for payment in created if id(payment) not in taken]
    return created, changed, list(adopted.items())
