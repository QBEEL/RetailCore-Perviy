"""Разбор кодов маркировки DataMatrix.

Код с пачки — это строка в формате GS1 из нескольких полей, склеенных подряд.
Каждое поле начинается с двузначного (иногда четырёхзначного) идентификатора
применения, дальше идёт значение. Поля постоянной длины читаются по счёту,
переменной — до разделителя GS (символ 0x1D) или до конца строки.

Разбор нужен потому, что глазами код не читается вовсе, а из него сразу видно
главное: какой это товар (GTIN) и какой конкретно экземпляр (серийный номер).
Половину вопросов «что это за коробка приехала» это закрывает без обращения к
Честному ЗНАКу.

Сканеры отдают код по-разному: кто-то ставит впереди признак символики `]d2`,
кто-то заменяет неотображаемый GS на видимую последовательность вроде `<GS>`
или `&#29;`. Разбор терпит все встреченные варианты — иначе пользователь видит
«код не распознан» на совершенно исправном коде.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from typing import Iterable

# Разделитель полей переменной длины.
GS = "\x1d"

# Как разделитель приходит от разных сканеров и из разных выгрузок.
GS_ALIASES = ("␝", "<GS>", "&#29;", "\\x1d", "\\u001d", "{GS}")

# Признак символики DataMatrix, который некоторые сканеры ставят в начало.
SYMBOLOGY = ("]d2", "]D2", "]d1", "]C1")

# Идентификаторы применения: длина значения, если она постоянная.
FIXED_LENGTH = {
    "00": 18,  # SSCC — код транспортной упаковки
    "01": 14,  # GTIN — код товара
    "11": 6, "13": 6, "15": 6, "17": 6,  # даты, ГГММДД
}

# Названия полей для показа человеку.
TITLES = {
    "00": "код упаковки",
    "01": "код товара",
    "10": "партия",
    "11": "дата производства",
    "17": "годен до",
    "21": "серийный номер",
    "91": "ключ проверки",
    "92": "значение проверки",
    "93": "проверочный код",
    "8005": "цена",
}

# Четырёхзначные идентификаторы, встречающиеся в маркировке.
LONG_CODES = ("8005", "8006", "3103", "3202")

# Криптохвост: поля проверки, которые «Честный ЗНАК» дописывает в конец кода, и
# длина их значений. Ноль означает «до конца строки» — значение проверки всегда
# последнее.
#
# Нужны они здесь вот зачем. Поле переменной длины кончается разделителем GS, но
# сканеры сплошь и рядом его не выдают, а при вставке из письма или таблицы он
# теряется и вовсе. Тогда серийный номер «съедает» весь хвост: из
# `...215Xtj9=4WZnWPI91EE1192...` получается серийный номер длиной в полсотни
# знаков, и система такой код не опознаёт. Хвост состоит только из этих полей и
# стоит последним — по этому его начало и находится.
TAIL_FIELDS: dict[str, int] = {"91": 4, "92": 0, "93": 4, "8005": 6}



class CodeProblem(Exception):
    """Код не разбирается. Текст пригоден для показа пользователю."""


@dataclass(slots=True)
class Code:
    """Разобранный код маркировки."""

    raw: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def gtin(self) -> str:
        return self.fields.get("01", "")

    @property
    def serial(self) -> str:
        return self.fields.get("21", "")

    @property
    def valid(self) -> bool:
        return not self.problems

    @property
    def identity(self) -> tuple[str, str]:
        """Что делает экземпляр уникальным: товар и его серийный номер."""
        return (self.gtin, self.serial)

    @property
    def ki(self) -> str:
        """Код идентификации: товар и серийный номер, без криптохвоста.

        Именно его ждёт система: с ключом и значением проверки (91, 92) запрос
        сведений отвечает отказом, а тот же код без них — сведениями. Проверено
        на живой системе кодом парфюмерии, снятым сканером.
        """
        if not self.gtin or not self.serial:
            return ""
        return f"01{self.gtin}21{self.serial}"

    @property
    def full(self) -> str:
        """Код для печати: код идентификации и криптохвост через разделители.

        Пусто, если хвоста нет. Этикетка без ключа и значения проверки читается
        сканером, но подлинность не подтверждает: приложение «Честного ЗНАКа»
        отвечает на неё «сомнительный товар». Разделители ставятся заново —
        сканер их часто не выдаёт, а в символе они обязательны.
        """
        if not self.valid or not any(self.fields.get(name)
                                     for name in ("91", "92", "93")):
            return ""
        names = list(self.fields)
        parts: list[str] = []
        for index, name in enumerate(names):
            parts.append(f"{name}{self.fields[name]}")
            # Разделитель закрывает поле переменной длины, если оно не последнее.
            if name not in FIXED_LENGTH and index < len(names) - 1:
                parts.append(GS)
        return "".join(parts)

    @property
    def ean13(self) -> str:
        """GTIN-14 → EAN-13, каким он лежит в каталоге и в прайсах.

        Первая цифра GTIN-14 — признак уровня упаковки. У штучного товара это
        ноль, и тогда остаток и есть привычный тринадцатизначный штрихкод.
        """
        if len(self.gtin) == 14 and self.gtin[0] == "0":
            return self.gtin[1:]
        return ""

    @property
    def title(self) -> str:
        return f"{self.gtin} · {self.serial}" if self.gtin else self.raw[:32]


# Сканер «печатает» код как клавиатура, и при русской раскладке Windows
# превращает клавиши в кириллицу: `21abc` приходит как `21фис`. В коде
# маркировки кириллицы не бывает — набор GS1 только латинский, — поэтому
# русская буква в коде значит одно: раскладка была русской, и знаки
# возвращаются по клавишам, на которых стоят. Знаки препинания тоже: в русской
# раскладке на клавише «/» точка, на Shift+4 — «;» и так далее. Цифры, «%»,
# «*», скобки, «-», «_», «+», «=» и «!» в обеих раскладках одни и те же.
_RUSSIAN_KEYS = str.maketrans(
    "йцукенгшщзхъфывапролджэячсмитьбюё"
    "ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮЁ"
    "\"№;:?.,/",
    "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
    "QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>~"
    "@#$^&/?|",
)
_CYRILLIC = re.compile("[а-яА-ЯёЁ№]")


def _unquote_csv(text: str) -> str:
    """Снимает внешние кавычки одной ячейки CSV (`"..."`) и сдвоенные `""` внутри кода."""
    value = (text or "").strip().lstrip("\ufeff")
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"' and "\n" not in value and "\r" not in value:
        try:
            rows = [next(csv.reader([value], delimiter=sep)) for sep in (",", ";", "\t")]
        except (csv.Error, StopIteration):
            rows = []
        if rows and all(len(row) == 1 for row in rows):
            inner = rows[0][0].strip()
            start = next((len(marker) for marker in SYMBOLOGY if inner.startswith(marker)), 0)
            if inner[start:start + 2].isdigit() or _CYRILLIC.search(inner):
                return inner
    return value


def from_keyboard(text: str) -> str:
    """Код, набранный сканером при русской раскладке, — обратно латиницей.

    Код без кириллицы не меняется: перекладывать его знаки препинания нельзя,
    точка в нём — это точка. С кириллицей перекладывается вся строка, иначе
    «/» осталась бы точкой. Но только если вышел код — латиница, начатая
    цифрами идентификатора: русский текст, который кодом не был, остаётся
    как есть, чтобы человек увидел то, что вставил.
    """
    text = _unquote_csv(text) if text and text.lstrip("\ufeff").strip().startswith('"') else text
    if not text or not _CYRILLIC.search(text):
        return text
    converted = text.translate(_RUSSIAN_KEYS)
    start = next((len(marker) for marker in SYMBOLOGY if converted.startswith(marker)), 0)
    if not converted.isascii() or not converted[start:start + 2].isdigit():
        return text
    return converted


def normalize(text: str) -> str:
    """Приводит код к виду, пригодному для разбора."""
    value = from_keyboard(_unquote_csv((text or "").strip()))
    for alias in GS_ALIASES:
        value = value.replace(alias, GS)
    for marker in SYMBOLOGY:
        if value.startswith(marker):
            value = value[len(marker):]
            break
    return value


def for_request(text: str) -> str:
    """Код в том виде, в каком его ждёт система маркировки.

    Отдаётся код идентификации — без ключа и значения проверки. Неразобранный
    код уходит как есть: решать за систему, что она его не поймёт, нельзя, а
    ответ по нему и есть результат проверки.
    """
    value = normalize(text)
    if not value:
        return ""
    return parse(value).ki or value


def gtin_valid(gtin: str) -> bool:
    """Контрольная цифра GTIN. Ошибка в ней означает искажённый код."""
    if len(gtin) != 14 or not gtin.isdigit():
        return False
    # Вес цифр чередуется 3 и 1, считая справа от контрольной.
    total = 0
    for index, digit in enumerate(reversed(gtin[:-1])):
        total += int(digit) * (3 if index % 2 == 0 else 1)
    return (10 - total % 10) % 10 == int(gtin[-1])


def parse(text: str) -> Code:
    """Разбирает один код. Непонятное складывается в `problems`, не в исключение."""
    raw = (text or "").strip()
    value = normalize(raw)
    code = Code(raw=raw)
    if not value:
        code.problems.append("пустая строка")
        return code

    position = 0
    while position < len(value):
        if value[position] == GS:
            position += 1
            continue
        identifier = _identifier(value, position)
        if identifier is None:
            code.problems.append(
                f"непонятное поле на позиции {position}: {value[position:position + 6]!r}")
            break
        position += len(identifier)
        content, position = _content(value, position, identifier)
        if identifier in code.fields:
            code.problems.append(f"поле {identifier} встречается дважды")
        code.fields[identifier] = content

    if not code.fields:
        code.problems.append("в коде нет ни одного поля")
        return code
    if "01" not in code.fields:
        code.problems.append("нет кода товара (01)")
    elif not gtin_valid(code.fields["01"]):
        code.problems.append(f"неверная контрольная цифра GTIN {code.fields['01']}")
    if "21" not in code.fields:
        code.problems.append("нет серийного номера (21)")
    return code


def _identifier(value: str, position: int) -> str | None:
    """Идентификатор применения в этой позиции: сначала длинные, потом обычные."""
    for candidate in LONG_CODES:
        if value.startswith(candidate, position):
            return candidate
    pair = value[position:position + 2]
    if len(pair) == 2 and pair.isdigit():
        return pair
    return None


def _content(value: str, position: int, identifier: str) -> tuple[str, int]:
    """Значение поля и позиция следующего."""
    if identifier in FIXED_LENGTH:
        length = FIXED_LENGTH[identifier]
        return value[position:position + length], position + length
    end = value.find(GS, position)
    if end != -1:
        return value[position:end], end + 1
    # Разделителя нет — ищем начало криптохвоста. Значение проверки (92) при
    # этом не трогаем: оно и есть последнее поле, а искать хвост внутри хвоста
    # значит разрезать подпись пополам на первом же похожем сочетании знаков.
    # Уже внутри хвоста (после ключа 91) значение проверки идёт без ключа
    # перед собой; в серийном номере с него хвост начаться не может.
    in_tail = identifier in TAIL_FIELDS
    if identifier != "92" and (
            tail := _tail_start(value, position, in_tail)) > position:
        return value[position:tail], tail
    return value[position:], len(value)


def _tail_start(value: str, position: int, in_tail: bool = False) -> int:
    """С какого места начинается криптохвост. -1, если его не видно."""
    for index in range(position, len(value)):
        if _is_tail(value, index, in_tail):
            return index
    return -1


def _is_tail(value: str, index: int, in_tail: bool = False) -> bool:
    """Разбирается ли остаток строки как криптохвост целиком.

    Проверяется весь остаток, а не одно совпадение: сочетания «91» и «92»
    встречаются и внутри серийного номера (в тринадцати знаках из почти
    сотни символов это случается у каждого сотого кода), и принять такое за
    начало хвоста значит обрезать серийный номер и получить «код не найден».
    Поэтому хвост должен выглядеть как настоящий:
      * начинается с ключа, а не со значения проверки — подпись без ключа
        не бывает (`in_tail`: когда ключ уже разобран, подпись и есть то, что
        следует за ним);
      * поля не повторяются;
      * кончается значением проверки (92) или проверочным кодом 93: один
        ключ без подписи не бывает, а «91» и четыре знака в самом конце
        серийного номера — бывают.
    Код без хвоста вовсе — тот, что в УПД, — хвоста не имеет, и ничто внутри
    его серийного номера им не считается.
    """
    position = index
    seen: set[str] = set()
    while position < len(value):
        identifier = ("8005" if value.startswith("8005", position)
                      else value[position:position + 2])
        if identifier not in TAIL_FIELDS or identifier in seen:
            return False
        if not seen and identifier == "92" and not in_tail:
            return False
        seen.add(identifier)
        position += len(identifier)
        length = TAIL_FIELDS[identifier]
        if not length:
            # Значение проверки идёт до конца строки.
            return position < len(value)
        if len(value) - position < length:
            return False
        position += length
    return "93" in seen


# --- пачка кодов ----------------------------------------------------------------

@dataclass(slots=True)
class Batch:
    """Результат разбора списка кодов."""

    codes: list[Code] = field(default_factory=list)

    @property
    def valid(self) -> list[Code]:
        return [code for code in self.codes if code.valid]

    @property
    def broken(self) -> list[Code]:
        return [code for code in self.codes if not code.valid]

    @property
    def duplicates(self) -> dict[tuple[str, str], list[Code]]:
        """Экземпляры, встреченные больше одного раза.

        Один и тот же серийный номер в двух местах партии — это либо пересчёт,
        либо копия кода. Ни то ни другое нельзя оставлять незамеченным, и
        замечается это без всякого обращения к Честному ЗНАКу.
        """
        seen: dict[tuple[str, str], list[Code]] = {}
        for code in self.valid:
            seen.setdefault(code.identity, []).append(code)
        return {key: found for key, found in seen.items() if len(found) > 1}

    @property
    def by_gtin(self) -> dict[str, list[Code]]:
        grouped: dict[str, list[Code]] = {}
        for code in self.valid:
            grouped.setdefault(code.gtin, []).append(code)
        return grouped

    @property
    def summary(self) -> str:
        parts = [f"кодов: {len(self.codes)}"]
        if self.broken:
            parts.append(f"не разобрано: {len(self.broken)}")
        parts.append(f"товаров: {len(self.by_gtin)}")
        if self.duplicates:
            parts.append(f"повторов: {len(self.duplicates)}")
        return " · ".join(parts)


def parse_many(lines: Iterable[str]) -> Batch:
    """Разбирает список кодов. Пустые строки пропускаются."""
    return Batch(codes=[parse(line) for line in lines if (line or "").strip()])


_CSV_HEADER_NAMES = frozenset({
    "ки", "кин", "киз", "код", "код маркировки", "коды маркировки",
    "код идентификации", "код идентификации (ки)", "код набора",
    "код набора (кин)", "код упаковки", "код товара", "gtin", "cis", "ki",
    "kiz", "code", "codes", "sgtin", "sntin", "uit", "уит", "datamatrix",
    "штрихкод", "марка", "набор", "статус", "status", "наименование",
    "наименование товара", "товар", "серийный номер", "serial", "тип упаковки",
    "package_type", "packagetype", "cistype", "№", "id", "number",
})


def _is_clean_single_code(text: str) -> bool:
    """Является ли строка одним кодом маркировки без приклеенных колонок CSV."""
    if not text or "\t" in text:
        return False
    parsed = parse(text)
    if not parsed.valid or not parsed.ki:
        return False
    if "92" in parsed.fields:
        tail_sig = parsed.fields["92"]
        return not any(sep in tail_sig for sep in (";", ",", " ", "\t", '"'))
    if "93" in parsed.fields:
        return True
    serial = parsed.serial
    if not serial or len(serial) > 20 or " " in serial or _CYRILLIC.search(serial):
        return False
    if ";" in serial or "," in serial:
        # Для КИ без криптохвоста серийный номер в ЧЗ имеет длину 6, 7 или 13 символов.
        # Если вся строка уже имеет стандартную длину КИ (24, 25 или 31 символ),
        # то `;` или `,` внутри неё — это часть серийного номера GS1-82, а не разделитель CSV.
        if len(serial) in (6, 7, 13):
            return True
        for sep in (";", ","):
            if sep in text:
                first_cell = text.split(sep, 1)[0].strip()
                first_parsed = parse(first_cell)
                if first_parsed.valid and len(first_parsed.serial) >= 6:
                    return False
    return True


def _starts_like_gs1_code(text: str) -> bool:
    value = _unquote_csv(text).strip()
    for marker in SYMBOLOGY:
        if value.startswith(marker):
            value = value[len(marker):]
            break
    return (value.startswith("01") or value.startswith("00")) and value[:6].isdigit()


def _is_csv_header_row(cells: list[str], line_index: int, total_lines: int) -> bool:
    cleaned = [cell.strip().strip('"').strip().lower() for cell in cells if cell.strip()]
    if not cleaned:
        return True
    if any(_starts_like_gs1_code(cell) for cell in cells):
        return False
    if any(cell in _CSV_HEADER_NAMES for cell in cleaned):
        return line_index == 0 or len(cleaned) > 1
    return False


def split_lines(text: str) -> list[str]:
    """Делит вставленный текст или содержимое CSV-файла на коды маркировки.

    Сканер обычно завершает код переводом строки, а при загрузке из CSV или
    вставке из таблицы коды могут быть заключены в кавычки (`"..."` со
    сдвоенными `""`), сопровождаться шапкой таблицы и соседними колонками
    через `;`, `,` или табуляцию.
    """
    raw_lines = [
        line.strip().lstrip("\ufeff")
        for line in re.split(r"[\r\n]+", text or "")
        if line.strip().lstrip("\ufeff")
    ]
    result: list[str] = []
    total = len(raw_lines)
    for idx, raw_line in enumerate(raw_lines):
        unquoted = from_keyboard(_unquote_csv(raw_line))
        if _is_clean_single_code(unquoted):
            result.append(unquoted)
            continue

        # Пробуем разобрать строку как строку CSV/TSV с несколькими ячейками.
        cells: list[str] = [unquoted]
        for sep in ("\t", ";", ","):
            if sep in raw_line:
                try:
                    parsed_row = next(csv.reader([raw_line], delimiter=sep))
                except (csv.Error, StopIteration):
                    continue
                non_empty = [from_keyboard(_unquote_csv(c)) for c in parsed_row if c.strip()]
                if len(non_empty) > 1:
                    cells = non_empty
                    break

        valid_cells = [cell for cell in cells if _is_clean_single_code(cell) or parse(cell).valid]
        if valid_cells:
            result.extend(valid_cells)
            continue

        if _is_csv_header_row(cells, idx, total):
            continue

        # Если в строке есть ячейка, начинающаяся как GS1-код (с ошибкой), отдаём её.
        gs1_like = [cell for cell in cells if _starts_like_gs1_code(cell)]
        if gs1_like:
            result.extend(gs1_like)
        elif "\t" in raw_line:
            result.extend(cells)
        else:
            result.append(unquoted)
    return result


def read_codes_file(path: str) -> str:
    """Читает текстовый или CSV-файл с кодами маркировки с автоопределением кодировки."""
    with open(path, "rb") as raw_file:
        data = raw_file.read()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
