"""GS1 DataMatrix (ECC 200) для этикеток с кодами маркировки.

Код маркировки печатается именно таким символом: первым кодовым словом идёт
FNC1 — по нему сканер понимает, что внутри GS1, — а разделитель групп (GS,
`\\x1d`) между полями переменной длины записывается тем же FNC1. Готовых
кодировщиков в проекте нет, а тянуть ради одного символа библиотеку с
собственной нативной частью значило бы усложнить сборку под Windows и macOS.

Кодируется только в режиме ASCII: пары цифр уплотняются, остальное идёт по
одному знаку. Это не самый плотный из режимов (C40 и Base 256 дают меньше), но
стандартный и читаемый любым сканером; на размере этикетки 43×25 мм символ
получается 32×32 – 36×36 модулей и при 203 dpi печатается по 3 точки на модуль.

Квадратные символы 10×10 … 52×52 — прямоугольные и большие для маркировки не нужны.
Каждый размер сверен с декодером libdmtx на случайных кодах маркировки.
"""
from __future__ import annotations

from dataclasses import dataclass

FNC1 = 232
GS = "\x1d"

# Знаки в начале строки, которые некоторые сканеры дописывают сами: символика
# GS1 DataMatrix и FNC1. В кодируемые данные они не входят.
_PREFIXES = ("]d2", "]d1")


@dataclass(frozen=True, slots=True)
class _Size:
    """Квадратный символ: размер, ёмкость, блоки Рида — Соломона и области."""

    side: int
    data: int
    check: int
    blocks: int
    regions: int  # областей данных по каждой стороне

    @property
    def region_side(self) -> int:
        return (self.side - 2 * self.regions) // self.regions

    @property
    def mapping(self) -> int:
        """Сторона матрицы размещения — без поисковых шаблонов."""
        return self.side - 2 * self.regions


# Таблица ISO/IEC 16022 для квадратных символов до 52×52. Большие символы
# (четыре области данных и больше) не проверены декодером, а коду маркировки
# они и не нужны: он укладывается в 48×48 — поэтому в таблицу не включены.
_SIZES: tuple[_Size, ...] = (
    _Size(10, 3, 5, 1, 1), _Size(12, 5, 7, 1, 1), _Size(14, 8, 10, 1, 1),
    _Size(16, 12, 12, 1, 1), _Size(18, 18, 14, 1, 1), _Size(20, 22, 18, 1, 1),
    _Size(22, 30, 20, 1, 1), _Size(24, 36, 24, 1, 1), _Size(26, 44, 28, 1, 1),
    _Size(32, 62, 36, 1, 2), _Size(36, 86, 42, 1, 2), _Size(40, 114, 48, 1, 2),
    _Size(44, 144, 56, 1, 2), _Size(48, 174, 68, 1, 2), _Size(52, 204, 84, 2, 2),
)


class DataMatrixError(ValueError):
    """Код нельзя уместить в символ."""


# --- поле GF(256) для Рида — Соломона ------------------------------------------------

_EXP = [0] * 512
_LOG = [0] * 256


def _tables() -> None:
    value = 1
    for power in range(255):
        _EXP[power] = value
        _LOG[value] = power
        value <<= 1
        if value & 0x100:
            value ^= 0x12D
    for power in range(255, 512):
        _EXP[power] = _EXP[power - 255]


_tables()


def _multiply(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(degree: int) -> list[int]:
    poly = [1]
    for power in range(1, degree + 1):
        shifted = poly + [0]
        for index, coefficient in enumerate(poly):
            shifted[index + 1] ^= _multiply(coefficient, _EXP[power])
        poly = shifted
    return poly


def _check_words(data: list[int], degree: int) -> list[int]:
    generator = _generator(degree)
    remainder = [0] * degree
    for word in data:
        factor = word ^ remainder[0]
        remainder = remainder[1:] + [0]
        if factor:
            for index in range(degree):
                remainder[index] ^= _multiply(generator[index + 1], factor)
    return remainder


# --- кодирование данных -------------------------------------------------------------

def _clean(text: str) -> str:
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _ascii_words(text: str) -> list[int]:
    """Данные в кодовых словах режима ASCII, с FNC1 в начале и вместо GS."""
    words = [FNC1]
    index = 0
    while index < len(text):
        char = text[index]
        if char == GS:
            words.append(FNC1)
            index += 1
            continue
        pair = text[index:index + 2]
        if len(pair) == 2 and pair.isdigit() and pair.isascii():
            words.append(130 + int(pair))
            index += 2
            continue
        code = ord(char)
        if code > 255:
            raise DataMatrixError(f"Знак «{char}» нельзя записать в код маркировки.")
        if code < 128:
            words.append(code + 1)
        else:
            words.extend((235, code - 127))
        index += 1
    return words


def _pad(words: list[int], capacity: int) -> list[int]:
    padded = list(words)
    if len(padded) < capacity:
        padded.append(129)
    while len(padded) < capacity:
        position = len(padded) + 1
        value = 129 + (149 * position) % 253 + 1
        if value > 254:
            value -= 254
        padded.append(value)
    return padded


def _pick_size(count: int) -> _Size:
    for size in _SIZES:
        if size.data >= count:
            return size
    raise DataMatrixError(
        f"Код слишком длинный для DataMatrix: {count} кодовых слов.")


def _with_checks(words: list[int], size: _Size) -> list[int]:
    """Блоки данных и проверочные слова, перемешанные по стандарту."""
    blocks = size.blocks
    per_check = size.check // blocks
    lengths = [size.data // blocks] * blocks
    parts: list[list[int]] = []
    start = 0
    for length in lengths:
        parts.append(words[start:start + length])
        start += length
    checks = [_check_words(part, per_check) for part in parts]

    result = [0] * (size.data + size.check)
    for block, part in enumerate(parts):
        for index, word in enumerate(part):
            result[index * blocks + block] = word
    for block, check in enumerate(checks):
        for index, word in enumerate(check):
            result[size.data + index * blocks + block] = word
    return result


# --- размещение кодовых слов (приложение F стандарта) --------------------------------

def _place(words: list[int], rows: int, cols: int) -> list[list[int]]:
    grid = [[-1] * cols for _ in range(rows)]

    def module(row: int, col: int, position: int, bit: int) -> None:
        if row < 0:
            row += rows
            col += 4 - ((rows + 4) % 8)
        if col < 0:
            col += cols
            row += 4 - ((cols + 4) % 8)
        grid[row][col] = 1 if words[position] & (1 << (8 - bit)) else 0

    def utah(row: int, col: int, position: int) -> None:
        module(row - 2, col - 2, position, 1)
        module(row - 2, col - 1, position, 2)
        module(row - 1, col - 2, position, 3)
        module(row - 1, col - 1, position, 4)
        module(row - 1, col, position, 5)
        module(row, col - 2, position, 6)
        module(row, col - 1, position, 7)
        module(row, col, position, 8)

    def corner1(position: int) -> None:
        module(rows - 1, 0, position, 1)
        module(rows - 1, 1, position, 2)
        module(rows - 1, 2, position, 3)
        module(0, cols - 2, position, 4)
        module(0, cols - 1, position, 5)
        module(1, cols - 1, position, 6)
        module(2, cols - 1, position, 7)
        module(3, cols - 1, position, 8)

    def corner2(position: int) -> None:
        module(rows - 3, 0, position, 1)
        module(rows - 2, 0, position, 2)
        module(rows - 1, 0, position, 3)
        module(0, cols - 4, position, 4)
        module(0, cols - 3, position, 5)
        module(0, cols - 2, position, 6)
        module(0, cols - 1, position, 7)
        module(1, cols - 1, position, 8)

    def corner3(position: int) -> None:
        module(rows - 3, 0, position, 1)
        module(rows - 2, 0, position, 2)
        module(rows - 1, 0, position, 3)
        module(0, cols - 2, position, 4)
        module(0, cols - 1, position, 5)
        module(1, cols - 1, position, 6)
        module(2, cols - 1, position, 7)
        module(3, cols - 1, position, 8)

    def corner4(position: int) -> None:
        module(rows - 1, 0, position, 1)
        module(rows - 1, cols - 1, position, 2)
        module(0, cols - 3, position, 3)
        module(0, cols - 2, position, 4)
        module(0, cols - 1, position, 5)
        module(1, cols - 3, position, 6)
        module(1, cols - 2, position, 7)
        module(1, cols - 1, position, 8)

    position, row, col = 0, 4, 0
    while True:
        if row == rows and col == 0:
            corner1(position)
            position += 1
        if row == rows - 2 and col == 0 and cols % 4:
            corner2(position)
            position += 1
        if row == rows - 2 and col == 0 and cols % 8 == 4:
            corner3(position)
            position += 1
        if row == rows + 4 and col == 2 and cols % 8 == 0:
            corner4(position)
            position += 1
        while True:
            if row < rows and col >= 0 and grid[row][col] == -1:
                utah(row, col, position)
                position += 1
            row -= 2
            col += 2
            if not (row >= 0 and col < cols):
                break
        row += 1
        col += 3
        while True:
            if row >= 0 and col < cols and grid[row][col] == -1:
                utah(row, col, position)
                position += 1
            row += 2
            col -= 2
            if not (row < rows and col >= 0):
                break
        row += 3
        col += 1
        if not (row < rows or col < cols):
            break

    # Незаполненный правый нижний угол закрашивается шахматным узором.
    if grid[rows - 1][cols - 1] == -1:
        grid[rows - 1][cols - 1] = 1
        grid[rows - 2][cols - 2] = 1
    return grid


# --- сборка символа -----------------------------------------------------------------

def encode(text: str) -> list[list[bool]]:
    """Матрица модулей GS1 DataMatrix для строки кода маркировки.

    `True` — тёмный модуль. Тихая зона вокруг символа не добавляется: её
    оставляет макет этикетки.
    """
    data = _clean(text)
    if not data:
        raise DataMatrixError("Пустой код.")
    words = _ascii_words(data)
    size = _pick_size(len(words))
    words = _with_checks(_pad(words, size.data), size)

    mapping = _place(words, size.mapping, size.mapping)
    side = size.side
    symbol = [[False] * side for _ in range(side)]
    step = size.region_side + 2

    for region_row in range(size.regions):
        for region_col in range(size.regions):
            top, left = region_row * step, region_col * step
            for index in range(step):
                symbol[top + step - 1][left + index] = True       # нижняя сплошная
                symbol[top + index][left] = True                  # левая сплошная
                symbol[top][left + index] = index % 2 == 0        # верхняя пунктирная
                symbol[top + index][left + step - 1] = index % 2 == 1
            for y in range(size.region_side):
                for x in range(size.region_side):
                    source_row = region_row * size.region_side + y
                    source_col = region_col * size.region_side + x
                    symbol[top + 1 + y][left + 1 + x] = mapping[source_row][source_col] == 1
    return symbol


def side_for(text: str) -> int:
    """Сторона символа в модулях — чтобы заранее подобрать размер точки."""
    return _pick_size(len(_ascii_words(_clean(text)))).side
