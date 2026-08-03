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


def normalize(text: str) -> str:
    """Приводит код к виду, пригодному для разбора."""
    value = (text or "").strip()
    for alias in GS_ALIASES:
        value = value.replace(alias, GS)
    for marker in SYMBOLOGY:
        if value.startswith(marker):
            value = value[len(marker):]
            break
    return value


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
    if end == -1:
        return value[position:], len(value)
    return value[position:end], end + 1


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


def split_lines(text: str) -> list[str]:
    """Делит вставленный текст на коды.

    Сканер обычно завершает код переводом строки, но при вставке из письма или
    таблицы разделителем оказывается что угодно. Внутри самого кода перевода
    строки быть не может, а вот пробел — может, поэтому по пробелам не делим.
    """
    return [line for line in re.split(r"[\r\n\t]+", text or "") if line.strip()]
