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
from dataclasses import replace
from datetime import date, datetime
from typing import Callable, Iterable, Sequence

from ..normalize import normalize_text
from .models import (
    AMOUNT_EPSILON,
    ImportReport,
    Payment,
    PaymentOrigin,
    PaymentStatus,
    RowChange,
    SUPPLIER_OPERATION,
    money_text,
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
    Для записей, у которых сумма уйдёт, отчёт запоминает, в какую сторону.
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
        else:
            # Предпросмотр считает по тому, что запишет импорт: поправленная
            # человеком сумма, которую 1С не меняла, останется как есть.
            coming = replace(payment)
            if getattr(found, "origin", "") == PaymentOrigin.IMPORT.value:
                _keep_manual_amount(coming, found)
            if _differs(coming, found):
                report.updated += 1
                stored = getattr(found, "values", {}).get("amount")
                if stored is not None:
                    report.note_amount(float(stored), coming.amount)
                report.details.append(_row_change(coming, found))
            else:
                report.same += 1
        report.payments.append(payment)
    taken = adoptions(fresh, candidates)
    report.adopted = len(taken)
    replaced = {id(payment) for payment in taken.values()}
    for candidate in candidates:
        if candidate.id in taken:
            report.note_amount(candidate.amount, taken[candidate.id].amount)
            report.details.append(_plan_change(candidate, taken[candidate.id]))
    for payment in fresh:
        if id(payment) not in replaced:
            report.details.append(RowChange(
                "new", payment.doc_number, payment.request_date, payment.recipient,
                payment.amount, fields=_new_fields(payment), key=payment.key))
    report.details.sort(key=lambda row: (
        ("plan", "changed", "new").index(row.kind), -abs(row.delta), row.doc_number))
    report.new = len(fresh) - report.adopted
    dates = [p.pay_date for p in report.payments if p.pay_date]
    report.first_pay = min(dates) if dates else None
    report.last_pay = max(dates) if dates else None
    report.recipients = len({recipient_key(p.recipient) for p in report.payments if p.recipient})
    return report


# Поля выгрузки, о которых предпросмотр говорит словами: название, вид.
# Сумма в список не входит — у неё свои колонки «было» и «стало».
FIELD_TITLES: dict[str, tuple[str, str]] = {
    "pay_date": ("Дата платежа", "date"),
    "source_status": ("Статус в 1С", "text"),
    "paid_flag": ("Оплачена", "flag"),
    "recipient": ("Получатель", "text"),
    "vat": ("НДС, ₽", "money"),
    "operation": ("Операция", "text"),
    "over_limit": ("Сверх лимита", "flag"),
    "priority": ("Приоритет", "text"),
    "edo_state": ("Состояние ЭДО", "text"),
    "responsible": ("Заявитель", "text"),
    "author": ("Автор", "text"),
    "currency": ("Валюта", "text"),
    "had_files": ("Есть файлы", "flag"),
}


def _field_changes(payment: Payment, existing: object) -> list[tuple[str, object, object]]:
    """Поля из 1С, которые у записи в базе отличаются: (поле, было, стало)."""
    from .store import IMPORTED_FIELDS, imported_values

    values = imported_values(payment)
    stored = getattr(existing, "values", {})
    found: list[tuple[str, object, object]] = []
    for name in IMPORTED_FIELDS:
        before, after = stored.get(name), values.get(name)
        if isinstance(after, float) or isinstance(before, float):
            different = abs(float(before or 0.0) - float(after or 0.0)) > 0.005
        else:
            different = str(before or "") != str(after or "")
        if different:
            found.append((name, before, after))
    return found


def _paid_in_1c(payment: Payment, existing: object) -> bool:
    """1С считает заявку оплаченной, а в базе статус другой.

    Отметка об оплате лежит среди полей из 1С, а статус — нет: его могли
    назначить вручную («Перенесено»). Но факт оплаты сильнее любого статуса,
    поэтому оплаченная в 1С заявка получает «Оплачено» и из просрочки, и из
    переноса. Это же лечит записи, у которых отметка уже дошла, а статус остался.
    """
    return payment.paid_flag and getattr(existing, "status", "") != PaymentStatus.PAID.value


def _differs(payment: Payment, existing: object) -> bool:
    """Отличается ли запись от лежащей в базе по полям из 1С."""
    return bool(_field_changes(payment, existing)) or _paid_in_1c(payment, existing)


def _show(kind: str, value: object) -> str:
    """Значение поля глазами человека: дата — «01.10.2026», флаг — «Да»."""
    if value is None or value == "":
        return ""
    if kind == "date":
        try:
            return f"{date.fromisoformat(str(value)[:10]):%d.%m.%Y}"
        except ValueError:
            return str(value)
    if kind == "flag":
        return "Да" if str(value).lower() in {"1", "true", "да"} else "Нет"
    if kind == "money":
        return money_text(float(value))
    return str(value)


def _row_change(payment: Payment, existing: object) -> RowChange:
    """Что импорт изменит в записи, уже лежащей в базе."""
    stored = getattr(existing, "values", {})
    row = RowChange(
        "changed", payment.doc_number, payment.request_date, payment.recipient,
        payment.amount, key=payment.key,
        amount_before=float(stored["amount"]) if stored.get("amount") is not None else None)
    changes = _field_changes(payment, existing)
    amount_moved = any(name == "amount" for name, _, _ in changes)
    for name, before, after in changes:
        # НДС идёт за суммой сам — отдельной строкой он только заслоняет суть.
        if name == "amount" or (name == "vat" and amount_moved):
            continue
        title, kind = FIELD_TITLES.get(name, (name, "text"))
        if name == "recipient":
            row.recipient = str(before or payment.recipient)
        row.fields.append((title, _show(kind, before), _show(kind, after)))
    if _paid_in_1c(payment, existing):
        was = next((s for s in PaymentStatus if s.value == getattr(existing, "status", "")), None)
        row.fields.append(("Статус", was.title if was else "", PaymentStatus.PAID.title))
    return row


def _plan_change(candidate: Payment, payment: Payment) -> RowChange:
    """Заявка 1С встаёт на место плановой или ручной оплаты."""
    row = RowChange(
        "plan", payment.doc_number, payment.request_date, payment.recipient,
        payment.amount, amount_before=candidate.amount, key=payment.key,
        note="вместо " + ("плана" if candidate.origin is PaymentOrigin.PLAN else "ручной записи"))
    if candidate.pay_date != payment.pay_date:
        row.fields.append(("Дата платежа", _show("date", candidate.pay_date and
                           candidate.pay_date.isoformat()), _show("date", payment.pay_date and
                           payment.pay_date.isoformat())))
    return row


def _new_fields(payment: Payment) -> list[tuple[str, str, str]]:
    """У новой заявки «было» нет — показываем, на что она встала."""
    fields = []
    if payment.pay_date:
        fields.append(("Дата платежа", "", _show("date", payment.pay_date.isoformat())))
    if payment.source_status:
        fields.append(("Статус в 1С", "", payment.source_status))
    return fields


def _keep_manual_amount(payment: Payment, found: object) -> None:
    """Сумма, поправленная человеком, переживает импорт — пока 1С её не сменила.

    В 1С всё та же сумма, что была до правки, — значит, правка новее выгрузки,
    и перезаписать её означало бы молча откатить решение человека каждым
    понедельничным импортом. Пришла другая сумма — она новее правки и
    берётся как есть. Сервер применяет то же правило сам; здесь оно нужно,
    чтобы предпросмотр не обещал изменить то, что не изменится.

    Пометку, поставленную самой выгрузкой, это не касается: вернуть в 1С
    прежнюю сумму — такое же новое значение, как и любое другое.
    """
    before = getattr(found, "amount_before", None)
    if before is None or getattr(found, "amount_by_import", False):
        return
    if abs(float(before) - payment.amount) >= AMOUNT_EPSILON:
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
    («855 876»). Если на день у получателя несколько открытых записей, заявки
    делятся между ними так, чтобы суммарное расхождение сумм вышло наименьшим
    (`_pair_by_amount`), а не по очереди строк файла: иначе первая же заявка
    забирала бы «свою» запись у той, которой она подходит лучше. Заявка без
    пары остаётся новой: сумма из 1С всё равно главнее, и терять её нельзя.

    Возвращает номер записи в базе → заявку, которая её заменит.
    """
    buckets: dict[tuple[str, date], list[Payment]] = {}
    for candidate in candidates:
        if candidate.doc_number or candidate.pay_date is None or not candidate.status.open:
            continue
        buckets.setdefault(
            (recipient_key(candidate.recipient), candidate.pay_date), []).append(candidate)
    requests: dict[tuple[str, date], list[Payment]] = {}
    for payment in fresh:
        if payment.pay_date is None:
            continue
        key = (recipient_key(payment.recipient), payment.pay_date)
        if key in buckets:
            requests.setdefault(key, []).append(payment)
    found: dict[int, Payment] = {}
    for key, group in requests.items():
        for candidate, payment in _pair_by_amount(buckets[key], group):
            found[candidate.id] = payment
    return found


def _pair_by_amount(
    candidates: Sequence[Payment],
    requests: Sequence[Payment],
) -> list[tuple[Payment, Payment]]:
    """Пары «запись, заявка» с наименьшей суммой расхождений сумм.

    Берётся min(записей, заявок) пар; лишние остаются без пары. Для разницы по
    модулю лучшее сопоставление не пересекается: если обе стороны отсортировать
    по сумме, то пары идут по порядку, и остаётся выбрать, какие из более
    длинного ряда взять. Это решается таблицей по префиксам. Равные суммы
    разводятся номером записи и порядком заявок в файле — результат не зависит
    от случайного порядка.
    """
    left = sorted(candidates, key=lambda c: (c.amount, c.id))
    right = sorted(enumerate(requests), key=lambda item: (item[1].amount, item[0]))
    swapped = len(left) > len(right)
    short, long = (right, left) if swapped else (left, right)
    rows, columns = len(short), len(long)

    def price(i: int, j: int) -> float:
        a = short[i][1].amount if swapped else short[i].amount
        b = long[j].amount if swapped else long[j][1].amount
        return abs(a - b)

    inf = float("inf")
    # best[i][j] — наименьшая цена, если первые i коротких ставим в первые j длинных.
    best = [[0.0 if i == 0 else inf for _ in range(columns + 1)] for i in range(rows + 1)]
    for i in range(1, rows + 1):
        for j in range(i, columns + 1):
            skip = best[i][j - 1]
            take = best[i - 1][j - 1] + price(i - 1, j - 1)
            best[i][j] = take if take <= skip else skip
    pairs: list[tuple[Payment, Payment]] = []
    i, j = rows, columns
    while i > 0:
        if best[i][j] == best[i - 1][j - 1] + price(i - 1, j - 1) and best[i - 1][j - 1] != inf:
            candidate = short[i - 1] if not swapped else long[j - 1]
            request = (long[j - 1][1] if not swapped else short[i - 1][1])
            pairs.append((candidate, request))
            i -= 1
        j -= 1
    return pairs


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
        # Правка человека новее и «защищённой» суммы, и присланной 1С.
        payment = _with_override(payment, report)
        if _differs(payment, found):
            changed.append((int(getattr(found, "id", 0)), payment))
    # Пары подбираются по суммам из 1С, а правка накладывается после: иначе
    # исправленная сумма меняла бы пары, которые человек видел в предпросмотре.
    adopted = adoptions(created, candidates)
    taken = {id(payment) for payment in adopted.values()}
    created = [_with_override(payment, report) for payment in created
               if id(payment) not in taken]
    return (created, changed,
            [(ident, _with_override(payment, report)) for ident, payment in adopted.items()])


def _with_override(payment: Payment, report: ImportReport) -> Payment:
    """Платёж с суммой, поправленной в предпросмотре; НДС идёт за суммой."""
    value = report.overrides.get(payment.key)
    if value is None or abs(value - payment.amount) < AMOUNT_EPSILON:
        return payment
    vat = round(payment.vat * value / payment.amount, 2) if payment.amount and payment.vat else payment.vat
    return replace(payment, amount=float(value), vat=vat)
