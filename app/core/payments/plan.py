"""План оплат из Excel: шаблон для менеджеров и разбор заполненного файла.

Категорийные менеджеры планируют оплаты у себя в Excel, а в программу их план
заносит один человек. Отсюда обе половины этого модуля: шаблон, который
раздаётся менеджерам, и разбор того, что они прислали.

Шаблон и разбор живут вместе намеренно. Заголовки колонок, названия листов и
формат периода — это одно соглашение, и разъехаться они не должны: файл,
созданный `write_template`, обязан читаться `read` без единой настройки.

Разбор ничего не пишет. Он возвращает отчёт, который показывается человеку, и
только подтверждённый отчёт применяется к базе, — как и импорт выгрузки 1С.

План менеджера за месяц заменяется целиком: присланный файл считается полной
картиной его месяца, а не добавкой к прошлой. Иначе отменённая менеджером
оплата осталась бы в календаре навсегда — вычёркивать строки из чужого плана
руками пришлось бы тому, кто грузит файлы. Оплаченное при замене не трогается
никогда: факт оплаты сильнее любого плана.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Sequence

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from ..workbook import open_workbook
from . import vat
from .importer import parse_amount, parse_date
from .models import (
    MONTHS,
    Payment,
    PaymentOrigin,
    PaymentStatus,
    SUPPLIER_OPERATION,
)
from .recipients import clean_name, guess_supplier, recipient_key

TEMPLATE_SHEET = "План оплат"
DIRECTORY_SHEET = "Справочник"

# Заголовки таблицы. Порядок — как в шаблоне; при разборе колонки ищутся по
# названию, а не по номеру: колонку легко подвинуть, а переименовать сложнее.
HEADERS: tuple[str, ...] = ("Дата", "Поставщик", "Сумма, ₽", "Ставка НДС", "Комментарий")

# Сколько пустых строк готовится в шаблоне. Триста — это месяц у самого
# нагруженного менеджера с запасом; лишние строки не мешают, а нехватку
# пришлось бы объяснять каждому.
TEMPLATE_ROWS = 300

# Сколько строк справочника охватывает выпадающий список.
DIRECTORY_ROWS = 1000

# Названия колонок, которые принимаются при разборе. Менеджер может прислать
# файл, собранный до появления шаблона, и «Получатель» вместо «Поставщика»
# отвергать незачем — это то же самое поле.
ALIASES: dict[str, tuple[str, ...]] = {
    "pay_date": ("дата", "дата оплаты", "дата платежа", "дата плана"),
    "supplier": ("поставщик", "получатель", "контрагент"),
    "amount": ("сумма", "сумма ₽", "сумма руб", "сумма к оплате", "оплата"),
    "rate": ("ставка ндс", "ндс", "ндс %", "ставка"),
    "comment": ("комментарий", "примечание", "основание", "заметка"),
}

# Без этих колонок файл не план оплат, а что-то другое.
REQUIRED: tuple[str, ...] = ("pay_date", "supplier", "amount")

# Метки шапки: кто планирует и на какой период. Период — подсказка человеку,
# месяцы плана всё равно берутся из дат строк.
MANAGER_LABELS: tuple[str, ...] = ("менеджер", "ответственный", "автор", "кто планирует")
PERIOD_LABELS: tuple[str, ...] = ("период", "месяц", "на месяц")

# Докуда искать шапку и заголовки таблицы. Менеджер мог вставить сверху пару
# строк со своими пометками, но не двадцать.
HEAD_LIMIT = 25

_SPACES = re.compile(r"[\s ]+")
_PERIOD_RE = re.compile(r"(\d{1,2})[.\-/](\d{4})")


class PlanProblem(ValueError):
    """Файл нельзя прочитать как план оплат."""


@dataclass(slots=True)
class PlanRow:
    """Строка плана — то, что менеджер написал, и то, что из неё вышло."""

    number: int = 0
    pay_date: date | None = None
    supplier: str = ""
    amount: float = 0.0
    percent: int = 0
    comment: str = ""
    supplier_id: int = 0
    matched: str = ""

    @property
    def period(self) -> tuple[int, int]:
        return (self.pay_date.year, self.pay_date.month) if self.pay_date else (0, 0)


@dataclass(slots=True)
class PlanFile:
    """Прочитанный файл: чей план, на какие месяцы и что в нём.

    `skipped` — строки, которые разобрать не вышло, с указанием номера строки.
    Они не мешают загрузить остальное: из тридцати строк одна пустая дата не
    повод возвращать файл менеджеру целиком.
    """

    path: str = ""
    manager: str = ""
    period: str = ""
    sheet: str = ""
    rows: list[PlanRow] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def total(self) -> float:
        return sum(row.amount for row in self.rows)

    @property
    def months(self) -> list[tuple[int, int]]:
        """Месяцы, которых касается план, — по датам строк.

        Именно они, а не период из шапки, определяют, что будет заменено:
        строка, поставленная на соседний месяц, иначе осталась бы вне замены и
        задвоилась бы при следующей загрузке.
        """
        return sorted({row.period for row in self.rows if row.pay_date is not None})

    @property
    def months_title(self) -> str:
        parts = [f"{MONTHS[month - 1].lower()} {year}" for year, month in self.months]
        return ", ".join(parts) if parts else "период не определён"


@dataclass(slots=True)
class PlanReport:
    """Что даст загрузка плана. Показывается до записи в базу."""

    plan: PlanFile = field(default_factory=PlanFile)
    created: list[Payment] = field(default_factory=list)
    updated: list[Payment] = field(default_factory=list)
    removed: list[Payment] = field(default_factory=list)
    same: list[Payment] = field(default_factory=list)
    paid: list[Payment] = field(default_factory=list)
    unlinked: int = 0
    applied: bool = False

    @property
    def changes(self) -> int:
        return len(self.created) + len(self.updated) + len(self.removed)

    @property
    def total(self) -> float:
        """Сумма плана после применения — то, что увидит бюджет месяца."""
        return sum(p.amount for p in [*self.created, *self.updated, *self.same, *self.paid])

    @property
    def summary(self) -> str:
        parts = [f"строк {len(self.plan.rows)}"]
        if self.created:
            parts.append(f"новых {len(self.created)}")
        if self.updated:
            parts.append(f"изменится {len(self.updated)}")
        if self.removed:
            parts.append(f"исчезнет {len(self.removed)}")
        if self.same:
            parts.append(f"без изменений {len(self.same)}")
        if self.paid:
            parts.append(f"уже оплачено {len(self.paid)}")
        if self.plan.skipped:
            parts.append(f"пропущено {len(self.plan.skipped)}")
        return " · ".join(parts)


def plan_ref(manager: str, year: int, month: int) -> str:
    """Метка плана в оплате: чей и за какой месяц.

    По ней план опознаётся при следующей загрузке. Имя нормализуется: в базе
    один и тот же человек записан десятком написаний, и совпадение по сырой
    строке рассыпалось бы на первом же лишнем пробеле.
    """
    return f"plan:{year}-{month:02d}:{recipient_key(manager)}"


# --- шаблон --------------------------------------------------------------------

def write_template(
    destination: str,
    *,
    suppliers: Sequence[str] = (),
    managers: Sequence[str] = (),
    manager: str = "",
    year: int = 0,
    month: int = 0,
) -> str:
    """Создаёт пустой шаблон плана. Возвращает путь к файлу.

    Поставщики и менеджеры выгружаются на отдельный лист и подставляются в
    ячейки выпадающим списком. Список не запрещающий: вписать поставщика,
    которого ещё нет в базе, можно — при загрузке он опознается по имени.
    Запрет означал бы, что нового поставщика нельзя запланировать вообще, а
    планируют как раз новых.
    """
    moment = date.today()
    year = year or moment.year
    month = month or moment.month
    # Порядок — не красота: на нём держится сужение списка (`_narrowing_list`).
    suppliers = sorted(suppliers, key=str.lower)
    workbook = openpyxl.Workbook()
    try:
        sheet = workbook.active
        sheet.title = TEMPLATE_SHEET
        book = workbook.create_sheet(DIRECTORY_SHEET)
        _fill_directory(book, suppliers, managers)
        _fill_template(sheet, manager=manager, year=year, month=month,
                       suppliers=len(suppliers[:DIRECTORY_ROWS]),
                       managers=len(managers[:DIRECTORY_ROWS]))
        workbook.save(destination)
        return destination
    finally:
        workbook.close()


def _fill_directory(
    sheet: Any,
    suppliers: Sequence[str],
    managers: Sequence[str],
) -> None:
    """Лист со списками. Он не для чтения человеком, но и не скрыт.

    Скрытый лист менеджеры находят и всё равно открывают, а вопрос «что это за
    файл со скрытым листом» приходит к тому, кто раздал шаблон.
    """
    sheet["A1"], sheet["B1"], sheet["C1"] = "Поставщики", "Менеджеры", "Ставки НДС"
    for cell in ("A1", "B1", "C1"):
        sheet[cell].font = Font(bold=True)
    for index, name in enumerate(suppliers[:DIRECTORY_ROWS], start=2):
        sheet.cell(row=index, column=1, value=name)
    for index, name in enumerate(managers[:DIRECTORY_ROWS], start=2):
        sheet.cell(row=index, column=2, value=name)
    for index, rate in enumerate(vat.RATES, start=2):
        sheet.cell(row=index, column=3, value=rate.title)
    sheet.column_dimensions["A"].width = 44
    sheet.column_dimensions["B"].width = 30
    sheet.column_dimensions["C"].width = 14
    sheet["E1"] = ("Лист заполняется программой при выдаче шаблона. "
                   "Править его не нужно — списки берутся отсюда.")
    sheet["E1"].font = Font(italic=True, size=10)


def _fill_template(
    sheet: Any,
    *,
    manager: str,
    year: int,
    month: int,
    suppliers: int,
    managers: int,
) -> None:
    """Шапка, заголовки и подготовленные пустые строки."""
    sheet["A1"] = "План оплат поставщикам"
    sheet["A1"].font = Font(bold=True, size=14)
    sheet["A2"], sheet["B2"] = "Менеджер", manager
    sheet["A3"], sheet["B3"] = "Период", f"{MONTHS[month - 1]} {year}"
    for cell in ("A2", "A3"):
        sheet[cell].font = Font(bold=True)
    sheet["A4"] = (
        "Заполните строки ниже: дата оплаты, поставщик и сумма. Поставщика "
        "проще искать так: впишите начало названия, нажмите Enter и откройте "
        "список — в нём останутся только подходящие. Пустая ставка "
        "НДС читается как 22 %. Строки без даты, поставщика или суммы "
        "пропускаются. Файл заменяет ваш план целиком за те месяцы, что в нём "
        "встретились, — уже оплаченное не затрагивается.")
    sheet["A4"].font = Font(italic=True, size=10)
    sheet["A4"].alignment = Alignment(wrap_text=True, vertical="top")
    sheet.merge_cells("A4:E4")
    sheet.row_dimensions[4].height = 60

    head = 6
    fill = PatternFill("solid", fgColor="EEF2FF")
    edge = Side(style="thin", color="C7D2FE")
    for column, title in enumerate(HEADERS, start=1):
        cell = sheet.cell(row=head, column=column, value=title)
        cell.font = Font(bold=True)
        cell.fill = fill
        cell.border = Border(bottom=edge)
    for column, width in enumerate((14, 44, 18, 14, 40), start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width
    sheet.freeze_panes = f"A{head + 1}"

    first, last = head + 1, head + TEMPLATE_ROWS
    for row in range(first, last + 1):
        sheet.cell(row=row, column=1).number_format = "DD.MM.YYYY"
        sheet.cell(row=row, column=3).number_format = "# ##0.00"

    _validate(sheet, f"B{first}:B{last}",
              _narrowing_list(f"B{first}", max(suppliers, 1)),
              "Поставщик",
              "Впишите начало названия и нажмите Enter — список сузится до "
              "подходящих. Нового поставщика впишите целиком.")
    _validate(sheet, f"D{first}:D{last}",
              f"'{DIRECTORY_SHEET}'!$C$2:$C${len(vat.RATES) + 1}",
              "Ставка НДС", "Пустая ячейка читается как 22 %.")
    if managers:
        _validate(sheet, "B2", f"'{DIRECTORY_SHEET}'!$B$2:$B${managers + 1}",
                  "Менеджер", "Тот, чей это план.")


def _narrowing_list(cell: str, count: int) -> str:
    """Список поставщиков, сужающийся до тех, что начинаются с вписанного.

    Поставщиков сотни, и листать их все ради одного — то, на что жалуются.
    Поиск в самом списке есть только в свежем Excel 365, поэтому сужение
    собрано формулами, которые понимает любой Excel: вписанное в ячейку
    становится префиксом, и список показывает только совпавший кусок
    справочника. Кусок сплошной, потому что справочник отсортирован без учёта
    регистра — как сравнивают MATCH и COUNTIF. Пустая ячейка даёт «*», то есть
    весь справочник; ничего не совпало — тоже весь, а не пустой список.

    Ссылка на ячейку относительная: Excel сдвигает её для каждой строки
    диапазона проверки, и каждая строка сужает список по своему тексту.
    """
    names = f"'{DIRECTORY_SHEET}'!$A$2:$A${count + 1}"
    prefix = f'{cell}&"*"'
    return (f"IF(COUNTIF({names},{prefix}),"
            f"OFFSET('{DIRECTORY_SHEET}'!$A$1,MATCH({prefix},{names},0),0,"
            f"COUNTIF({names},{prefix}),1),{names})")


def _validate(sheet: Any, cells: str, source: str, title: str, prompt: str) -> None:
    """Выпадающий список, не запрещающий свои значения.

    `showErrorMessage=False` — это и есть «список плюс свободный ввод»: Excel
    подсказывает, но не отвергает. С запретом менеджер не смог бы вписать
    поставщика, которого ещё нет в базе.
    """
    rule = DataValidation(type="list", formula1=source, allow_blank=True,
                          showErrorMessage=False)
    rule.promptTitle = title
    rule.prompt = prompt
    rule.showInputMessage = True
    sheet.add_data_validation(rule)
    rule.add(cells)


# --- разбор --------------------------------------------------------------------

def read(path: str, sheet_name: str | None = None) -> PlanFile:
    """Читает заполненный шаблон. В базу ничего не пишет."""
    workbook = open_workbook(path, data_only=True)
    try:
        sheet = _pick_sheet(workbook, sheet_name)
        rows = [list(values) for values in sheet.iter_rows(values_only=True)]
        plan = PlanFile(path=path, sheet=sheet.title)
    finally:
        workbook.close()

    _read_head(plan, rows)
    columns, header = _find_columns(rows)
    for number, values in enumerate(rows[header + 1:], start=header + 2):
        _read_row(plan, number, values, columns)
    if not plan.rows and not plan.skipped:
        raise PlanProblem(
            f"{plan.name}: в файле нет ни одной заполненной строки плана.")
    return plan


def _pick_sheet(workbook: Any, sheet_name: str | None) -> Any:
    if sheet_name and sheet_name in workbook.sheetnames:
        return workbook[sheet_name]
    if TEMPLATE_SHEET in workbook.sheetnames:
        return workbook[TEMPLATE_SHEET]
    # Первый лист, а не единственный: менеджеры добавляют к плану свои
    # расчёты отдельными листами, и это не повод отказываться от файла.
    return workbook.worksheets[0]


def _read_head(plan: PlanFile, rows: list[list[Any]]) -> None:
    """Менеджер и период из шапки. Их отсутствие — не ошибка."""
    for values in rows[:HEAD_LIMIT]:
        for index, value in enumerate(values):
            label = _key(value)
            if not label:
                continue
            nearby = _text(values[index + 1]) if index + 1 < len(values) else ""
            if not nearby:
                continue
            if not plan.manager and label in MANAGER_LABELS:
                plan.manager = nearby
            elif not plan.period and label in PERIOD_LABELS:
                plan.period = nearby


def _find_columns(rows: list[list[Any]]) -> tuple[dict[str, int], int]:
    """Номера колонок и строка заголовка.

    Ищется по названиям: порядок колонок менеджеры меняют, и привязка к номеру
    сломалась бы на первом же файле, где комментарий передвинут левее суммы.
    """
    for number, values in enumerate(rows[:HEAD_LIMIT]):
        found: dict[str, int] = {}
        for index, value in enumerate(values):
            if (name := _column_of(value)) and name not in found:
                found[name] = index
        if all(name in found for name in REQUIRED):
            return found, number
    raise PlanProblem(
        "Не нашёл таблицу плана: нужны колонки «Дата», «Поставщик» и «Сумма». "
        "Проще всего взять пустой шаблон из программы и заполнить его.")


def _column_of(value: Any) -> str:
    label = _key(value)
    if not label:
        return ""
    for name, aliases in ALIASES.items():
        if label in aliases:
            return name
    return ""


def _read_row(
    plan: PlanFile,
    number: int,
    values: Sequence[Any],
    columns: dict[str, int],
) -> None:
    """Разбирает одну строку. Пустая пропускается молча, кривая — с пояснением."""
    def cell(name: str) -> Any:
        index = columns.get(name, -1)
        return values[index] if 0 <= index < len(values) else None

    supplier = clean_name(_text(cell("supplier")))
    raw_date, raw_amount = cell("pay_date"), cell("amount")
    if not supplier and raw_date in (None, "") and raw_amount in (None, ""):
        return

    when = _as_date(raw_date)
    amount = _as_amount(raw_amount)
    missing = []
    if when is None:
        missing.append("дата")
    if not supplier:
        missing.append("поставщик")
    if amount <= 0:
        missing.append("сумма")
    if missing:
        plan.skipped.append(f"строка {number}: не заполнено — {', '.join(missing)}")
        return

    plan.rows.append(PlanRow(
        number=number,
        pay_date=when,
        supplier=supplier,
        amount=amount,
        percent=_as_percent(cell("rate")),
        comment=_text(cell("comment")),
    ))


def link_suppliers(plan: PlanFile, suppliers: dict[int, str]) -> int:
    """Привязывает строки к карточкам поставщиков. Возвращает число непривязанных.

    Непривязанная строка — не ошибка: оплата живёт и по текстовому имени, ровно
    как пришедшая из 1С. Но их число стоит показать: десяток строк без карточек
    обычно означает опечатку в имени, а не десяток новых поставщиков.
    """
    if not suppliers:
        return len(plan.rows)
    unlinked = 0
    cache: dict[str, tuple[int, str]] = {}
    for row in plan.rows:
        key = recipient_key(row.supplier)
        if key not in cache:
            guess = guess_supplier(row.supplier, suppliers)
            cache[key] = (guess.supplier_id, guess.name) if guess is not None else (0, "")
        row.supplier_id, row.matched = cache[key]
        if not row.supplier_id:
            unlinked += 1
    return unlinked


# --- сравнение с базой ---------------------------------------------------------

def payments_of(plan: PlanFile) -> list[Payment]:
    """Строки плана в виде оплат — какими они должны стать в базе."""
    return [
        Payment(
            amount=row.amount,
            pay_date=row.pay_date,
            status=PaymentStatus.PLANNED,
            recipient=row.supplier,
            supplier_id=row.supplier_id,
            vat=_vat_of(row),
            operation=SUPPLIER_OPERATION,
            responsible=plan.manager,
            comment=row.comment,
            origin=PaymentOrigin.PLAN,
            origin_ref=plan_ref(plan.manager, *row.period),
        )
        for row in plan.rows
    ]


def existing_of(
    payments: Iterable[Payment],
    manager: str,
    months: Sequence[tuple[int, int]],
) -> list[Payment]:
    """Прошлый план этого менеджера за эти месяцы — то, что подлежит замене.

    Отбор по метке плана, а не по ответственному: оплаты, заведённые менеджером
    вручную или пришедшие из 1С, планом не считаются и заменой не затрагиваются.
    """
    refs = {plan_ref(manager, year, month) for year, month in months}
    return [p for p in payments if p.origin is PaymentOrigin.PLAN and p.origin_ref in refs]


def compare(plan: PlanFile, existing: Sequence[Payment]) -> PlanReport:
    """Считает, что даст загрузка: что создать, что поправить, что убрать.

    Строка узнаётся по паре «поставщик и дата». Сумма в ключ не входит — иначе
    исправленная сумма выглядела бы как удаление одной оплаты и создание
    другой, и в отчёте это читалось бы неверно.
    """
    report = PlanReport(plan=plan)
    buckets: dict[tuple[str, date | None], list[Payment]] = {}
    for payment in existing:
        buckets.setdefault(
            (recipient_key(payment.recipient), payment.pay_date), []).append(payment)
    for group in buckets.values():
        # Открытые вперёд: если на день пришлись план и уже оплаченная строка,
        # менять надо план, а оплаченное оставить нетронутым.
        group.sort(key=lambda p: (not p.status.open, p.id))

    for wanted in payments_of(plan):
        group = buckets.get((recipient_key(wanted.recipient), wanted.pay_date))
        current = group.pop(0) if group else None
        if current is None:
            report.created.append(wanted)
            continue
        if not current.status.open:
            # Оплаченное не переписывается и не задваивается: строка плана
            # считается исполненной.
            report.paid.append(current)
            continue
        if _differs(current, wanted):
            report.updated.append(_merged(current, wanted))
        else:
            report.same.append(current)

    for group in buckets.values():
        for leftover in group:
            (report.removed if leftover.status.open else report.paid).append(leftover)
    return report


def _differs(current: Payment, wanted: Payment) -> bool:
    return (
        abs(current.amount - wanted.amount) > 0.005
        or abs(current.vat - wanted.vat) > 0.005
        or current.comment != wanted.comment
        or current.supplier_id != wanted.supplier_id
        or current.recipient != wanted.recipient
    )


def _merged(current: Payment, wanted: Payment) -> Payment:
    """Существующая оплата с новыми значениями из плана.

    Правится ровно то, что задаёт план. Статус не трогается: перенесённую
    вручную оплату загрузка плана не должна возвращать в «Запланировано» —
    дата у неё и так совпала, иначе строка сюда бы не попала.
    """
    current.amount = wanted.amount
    current.vat = wanted.vat
    current.comment = wanted.comment
    current.supplier_id = wanted.supplier_id
    current.recipient = wanted.recipient
    current.origin_ref = wanted.origin_ref
    return current


# --- разбор значений -----------------------------------------------------------

def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _SPACES.sub(" ", str(value)).strip()


def _key(value: Any) -> str:
    """Название колонки или метки в сравнимом виде."""
    text = _text(value).lower().replace("ё", "е").rstrip(":")
    return _SPACES.sub(" ", text.replace(",", " ").replace(".", " ")).strip()


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return parse_date(_text(value))


def _as_amount(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 2)
    return parse_amount(_text(value))


def _as_percent(value: Any) -> int:
    """Ставка НДС из ячейки. Пусто — действующая ставка, сейчас 22 %.

    Excel хранит «22%» числом 0,22, а менеджер может написать и «22», и «22 %»,
    и «Без НДС». Все четыре записи означают одно и то же.
    """
    if value is None or _text(value) == "":
        return vat.DEFAULT.percent
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        percent = round(number * 100) if 0 < number < 1 else round(number)
        return int(percent) if vat.of(int(percent)) is not None else 0
    text = _key(value)
    if "без" in text or text in ("0", "нет", "-"):
        return 0
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return vat.DEFAULT.percent
    percent = int(digits)
    return percent if vat.of(percent) is not None else 0


def _vat_of(row: PlanRow) -> float:
    rate = vat.of(row.percent) or vat.BY_PERCENT[0]
    return rate.vat_of(row.amount)


def period_of(text: str) -> tuple[int, int]:
    """Период из шапки: «Октябрь 2026» или «10.2026». Не разобрали — нули.

    Нужен для подсказки в отчёте: месяцы плана определяются датами строк, но
    расхождение с тем, что менеджер написал в шапке, стоит показать — обычно
    это забытый после копирования файла прошлый месяц.
    """
    lowered = text.lower().replace("ё", "е")
    if match := _PERIOD_RE.search(lowered):
        month, year = int(match.group(1)), int(match.group(2))
        return (year, month) if 1 <= month <= 12 else (0, 0)
    year = 0
    if digits := re.search(r"(20\d{2})", lowered):
        year = int(digits.group(1))
    for index, name in enumerate(MONTHS, start=1):
        if name.lower().replace("ё", "е")[:4] in lowered:
            return (year or date.today().year, index)
    return (0, 0)
