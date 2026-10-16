"""Ценники-этикетки для обклейки: чтение номенклатуры из Excel и штрихкоды.

Файл — выгрузка из 1С или рабочая таблица: номенклатура, характеристика,
штрихкод, цена и сколько этикеток напечатать. Колонки находятся по заголовкам,
порядок в файле не важен.

Штрихкод рисуется самим приложением, без сторонних библиотек. Во внутренних
кодах магазина («25891146», «258911184») не 13 цифр и контрольная сумма EAN не
сходится, поэтому выбирается по содержимому: код, который *является* EAN-13,
печатается как EAN-13, любой другой — как Code 128. Сканер выдаёт одни и те же
цифры в обоих случаях, так что на кассе разницы нет.

Здесь нет Qt: модуль проверяется тестами без окна.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Sequence

from . import workbook

# --- чтение файла -------------------------------------------------------------

# Заголовок → поле. Сравнение по нормализованному тексту: регистр, «ё» и
# пробелы вокруг не важны; допускаются привычные сокращения.
_HEADERS = {
    "name": ("номенклатура", "наименование", "название", "товар"),
    "variant": ("характеристика", "оттенок", "вариант"),
    "barcode": ("штрихкод", "штрих-код", "шк", "ean", "баркод"),
    "price": ("цена", "розничная цена", "цена розница"),
    "copies": ("количество", "кол-во", "колво", "кол", "тираж", "этикеток"),
}


@dataclass(slots=True)
class PriceLabel:
    """Одна строка файла: что печатать и сколько штук."""

    row: int
    name: str
    variant: str
    barcode: str
    price: Decimal | None
    copies: int
    # Почему строку нельзя напечатать. Пусто — можно.
    problem: str = ""
    # Цена со скидкой для шаблонов с двумя ценами; считается, в файле её нет.
    price2: Decimal | None = None

    @property
    def title(self) -> str:
        return f"{self.name}, {self.variant}" if self.variant else self.name

    @property
    def printable(self) -> bool:
        return not self.problem and self.copies > 0


class LabelFileError(ValueError):
    """Файл не годится. Текст пригоден для показа."""


def _key(value: Any) -> str:
    return re.sub(r"[\s.]+", " ", str(value or "").lower().replace("ё", "е")).strip()


def _find_header(rows: Sequence[Sequence[Any]]) -> tuple[int, dict[str, int]]:
    """Строка заголовков и номера колонок. Ищется в первых строках листа."""
    best: tuple[int, dict[str, int]] = (-1, {})
    for index, row in enumerate(rows[:15]):
        found: dict[str, int] = {}
        for column, cell in enumerate(row):
            text = _key(cell)
            if not text:
                continue
            for field, names in _HEADERS.items():
                if field not in found and text in names:
                    found[field] = column
        if len(found) > len(best[1]):
            best = (index, found)
    return best


def clean_barcode(value: Any) -> str:
    """Штрихкод строкой цифр и букв, как он набран в 1С.

    Excel отдаёт длинный код числом: `2009852968146` приходит как `int`, а после
    правки в ячейке — как `float`, и `str()` дал бы «2.009852968146e+12».
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        if value == int(value):
            return str(int(value))
        return ""
    text = str(value).strip().replace(" ", "")
    # Текст вида «2,009852968146E+12» бывает после выгрузки через CSV.
    if re.fullmatch(r"\d+[.,]?\d*[eE]\+?\d+", text):
        try:
            return str(int(Decimal(text.replace(",", "."))))
        except (InvalidOperation, ValueError):
            return text
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".")[0]
    return text


def _price(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
    else:
        text = re.sub(r"[^\d,.\-]", "", str(value)).replace(",", ".")
        if not text:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
    return number if number >= 0 else None


def _copies(value: Any) -> int | None:
    """Число этикеток; `None` — в ячейке не число."""
    if value is None or str(value).strip() == "":
        return 0
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip().replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None
    if number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def format_price(price: Decimal | None) -> str:
    """«469» для целой цены, «449,50» для дробной."""
    if price is None:
        return ""
    if price == price.to_integral_value():
        return f"{int(price)}"
    return f"{price:.2f}".replace(".", ",")


def discounted(price: Decimal, percent: float) -> Decimal:
    """Цена после скидки. Целая цена округляется до рубля, дробная — до копейки.

    Округление «как в школе» (0,5 вверх): 469 − 10 % = 422,1 → 422, а 445 − 10 % =
    400,5 → 401. На ценнике рубли без копеек привычнее, и касса округляет так же.
    """
    if not 0 < percent < 100:
        raise ValueError("скидка должна быть больше 0 и меньше 100 процентов")
    value = price * (Decimal(100) - Decimal(str(percent))) / Decimal(100)
    step = Decimal(1) if price == price.to_integral_value() else Decimal("0.01")
    return value.quantize(step, rounding=ROUND_HALF_UP)


def apply_discount(labels: Sequence[PriceLabel], percent: float | None) -> None:
    """Проставляет цену со скидкой всем позициям с ценой; `None` — убирает."""
    for item in labels:
        item.price2 = (discounted(item.price, percent)
                       if percent is not None and item.price is not None else None)


def parse_rows(rows: Sequence[Sequence[Any]]) -> list[PriceLabel]:
    """Позиции из строк листа. Строки с проблемой не пропадают, а помечаются."""
    header_index, columns = _find_header(rows)
    if header_index < 0 or "barcode" not in columns or "name" not in columns:
        raise LabelFileError(
            "В файле не найдены колонки «Номенклатура» и «Штрихкод». Первой "
            "строкой должны стоять заголовки: Номенклатура, Характеристика, "
            "Штрихкод, Цена, Количество.")
    # Без колонки «Количество» печатается по одной этикетке на позицию; с
    # колонкой пустая ячейка означает «не печатать», а не «одну».
    has_copies = "copies" in columns

    def cell(row: Sequence[Any], field: str) -> Any:
        column = columns.get(field)
        return row[column] if column is not None and column < len(row) else None

    labels: list[PriceLabel] = []
    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        if not any(value is not None and str(value).strip() for value in row):
            continue
        name = str(cell(row, "name") or "").strip()
        barcode = clean_barcode(cell(row, "barcode"))
        if not name and not barcode:
            continue
        copies = _copies(cell(row, "copies")) if has_copies else 1
        price = _price(cell(row, "price"))
        problem = ""
        if copies is None:
            problem, copies = "количество — не целое число", 0
        elif not barcode:
            problem = "нет штрихкода"
        elif not encodable(barcode):
            problem = "в штрихкоде недопустимые знаки"
        elif price is None:
            problem = "нет цены"
        labels.append(PriceLabel(
            row=offset, name=name, variant=str(cell(row, "variant") or "").strip(),
            barcode=barcode, price=price, copies=copies, problem=problem))
    if not labels:
        raise LabelFileError("В файле нет ни одной позиции под заголовками.")
    return labels


def load(path: str, sheet: str | None = None) -> list[PriceLabel]:
    rows, _ = workbook.read_raw(path, sheet)
    return parse_rows(rows)


# --- штрихкоды ----------------------------------------------------------------

# Ширины полос и пробелов Code 128, по порядку «полоса, пробел, …»; номер —
# значение символа. 103–105 — старты A, B, C. Остановка — отдельный символ из
# семи элементов.
_CODE128 = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 "
    "221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 "
    "221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
    "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 "
    "231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 "
    "231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
    "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 "
    "112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
    "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 "
    "114131 311141 411131 211412 211214 211232").split()
_STOP = "2331112"
_START_B, _START_C = 104, 105
_CODE_B, _CODE_C = 100, 99


def encodable(text: str) -> bool:
    """Помещается ли текст в Code 128 (набор B — печатные знаки ASCII)."""
    return bool(text) and all(32 <= ord(ch) < 127 for ch in text)


def _code128_values(text: str) -> list[int]:
    """Значения символов Code 128 с минимальным числом символов.

    Длинные цифровые участки пакуются парами (набор C): `2009852968146` так
    занимает 7 символов, а не 13, и штрих-код уже, а значит модуль крупнее.
    """
    values: list[int] = []
    mode = ""
    i, n = 0, len(text)
    while i < n:
        run = 0
        while i + run < n and text[i + run].isdigit() and text[i + run].isascii():
            run += 1
        # Пары выгодны с четырёх цифр; код из двух-трёх цифр — тоже весь парами.
        use_c = run >= 4 or (run >= 2 and run == n)
        if use_c:
            if run % 2:
                # Лишняя цифра идёт первой и кодируется набором B.
                if not mode:
                    values.append(_START_B)
                    mode = "B"
                elif mode == "C":
                    values.append(_CODE_B)
                    mode = "B"
                values.append(ord(text[i]) - 32)
                i += 1
                run -= 1
            if mode != "C":
                values.append(_START_C if not mode else _CODE_C)
                mode = "C"
            for k in range(0, run, 2):
                values.append(int(text[i + k:i + k + 2]))
            i += run
            continue
        if mode != "B":
            values.append(_START_B if not mode else _CODE_B)
            mode = "B"
        values.append(ord(text[i]) - 32)
        i += 1
    return values


def code128(text: str) -> list[int]:
    """Элементы штрихкода: ширины полос и пробелов в модулях, начиная с полосы."""
    if not encodable(text):
        raise ValueError("Code 128: недопустимые знаки")
    values = _code128_values(text)
    checksum = values[0]
    for position, value in enumerate(values[1:], start=1):
        checksum += value * position
    values.append(checksum % 103)
    widths: list[int] = []
    for value in values:
        widths += [int(ch) for ch in _CODE128[value]]
    widths += [int(ch) for ch in _STOP]
    return widths


# Кодирование EAN-13: первая цифра задаёт чётность левой половины.
_EAN_L = ("0001101", "0011001", "0010011", "0111101", "0100011",
          "0110001", "0101111", "0111011", "0110111", "0001011")
_EAN_G = ("0100111", "0110011", "0011011", "0100001", "0011101",
          "0111001", "0000101", "0010001", "0001001", "0010111")
_EAN_R = ("1110010", "1100110", "1101100", "1000010", "1011100",
          "1001110", "1010000", "1000100", "1001000", "1110100")
_EAN_FIRST = ("LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
              "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL")


def ean13_valid(text: str) -> bool:
    """13 цифр и верная контрольная цифра."""
    if len(text) != 13 or not (text.isascii() and text.isdigit()):
        return False
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(text[:12]))
    return (10 - total % 10) % 10 == int(text[12])


def ean13(text: str) -> list[int]:
    """Элементы EAN-13 в том же виде, что и у `code128`: 95 модулей."""
    if not ean13_valid(text):
        raise ValueError("EAN-13: неверная длина или контрольная цифра")
    parity = _EAN_FIRST[int(text[0])]
    bits = "101"
    for digit, kind in zip(text[1:7], parity):
        bits += (_EAN_L if kind == "L" else _EAN_G)[int(digit)]
    bits += "01010"
    for digit in text[7:]:
        bits += _EAN_R[int(digit)]
    bits += "101"
    return _run_lengths(bits)


def _run_lengths(bits: str) -> list[int]:
    widths: list[int] = []
    count, current = 0, "1"
    for bit in bits:
        if bit == current:
            count += 1
        else:
            widths.append(count)
            count, current = 1, bit
    widths.append(count)
    return widths


@dataclass(frozen=True, slots=True)
class Barcode:
    kind: str
    elements: tuple[int, ...]

    @property
    def modules(self) -> int:
        return sum(self.elements)

    def bars(self) -> list[tuple[int, int]]:
        """(отступ, ширина) каждой тёмной полосы в модулях."""
        result: list[tuple[int, int]] = []
        position = 0
        for index, width in enumerate(self.elements):
            if index % 2 == 0:
                result.append((position, width))
            position += width
        return result


def encode(text: str) -> Barcode:
    """EAN-13, если код им является, иначе Code 128."""
    if ean13_valid(text):
        return Barcode("EAN-13", tuple(ean13(text)))
    return Barcode("Code 128", tuple(code128(text)))
