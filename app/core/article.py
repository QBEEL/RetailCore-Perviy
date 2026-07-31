"""Разбор артикулов: альтернативы, базовый артикул, модификатор.

Один товар описывается артикулом по-разному, и обе формы встречаются в одном
и том же файле:

* ячейка перечисляет **несколько равноправных артикулов** одной номенклатуры —
  ``zrp0050perG32/zrp0010perG32``, ``zrp0010perG19\\zrp0050perG19``;
* артикул состоит из **базы и модификации** — ``ABC123/50``, ``Cream-01/50ml``.

Различить их по одному лишь разделителю нельзя, поэтому решает длина и состав
хвоста: короткий (``50``, ``50ml``, ``XL``) — это модификация, длинный
(``zrp0010perG32``) — самостоятельный артикул. Ошибка в эту сторону безопасна:
лишний вариант просто ничего не найдёт, тогда как склейка двух разных артикулов
в один базовый подставила бы цену чужого товара.

Разделители задаются вызывающей стороной — они приходят из настроек, чтобы
механизм работал с любым поставщиком без правки кода.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Quantity
from .normalize import DEFAULT_KEY_OPTIONS, KeyOptions, code_key, parse_quantity

# Разделители перечисления: ячейка содержит несколько артикулов.
DEFAULT_SEPARATORS = "/\\,;|"
# Разделители модификации: артикул состоит из базы и хвоста.
DEFAULT_MODIFIER_SEPARATORS = "/-_"

# Хвост длиннее — это уже самостоятельный артикул, а не «30», «50ml» или «XL».
MAX_MODIFIER_LENGTH = 6
# Часть короче считается обрывком: «/» на конце строки не создаёт вариант.
MIN_ARTICLE_LENGTH = 3


@dataclass(frozen=True, slots=True)
class Article:
    """Один вариант прочтения артикула."""

    raw: str
    key: str
    base_key: str
    modifier: str = ""
    quantity: Quantity | None = None

    @property
    def has_modifier(self) -> bool:
        return bool(self.modifier)

    def __bool__(self) -> bool:
        return bool(self.key)


def _pattern(separators: str) -> re.Pattern[str]:
    return re.compile(f"[{re.escape(separators)}\n\r\t]+") if separators else re.compile(r"[\n\r\t]+")


def split_articles(value: object, separators: str = DEFAULT_SEPARATORS) -> list[str]:
    """Самостоятельные артикулы из ячейки.

    Чисто числовой короткий хвост отбрасывается: в ``ABC123/30`` это модификация,
    и как отдельный артикул «30» совпал бы с чем попало.
    """
    text = _clean(value)
    if not text:
        return []
    pieces = [p.strip() for p in _pattern(separators).split(text) if p.strip()]
    # Единственная часть — это и есть артикул, каким бы коротким он ни был:
    # у поставщика встречается чисто числовой артикул, и отбросить его нельзя.
    if len(pieces) < 2:
        return pieces
    # Модификацией может быть только хвост: в «ABC123/30» это «30», а в
    # «A-1|B-2» обе части — полноценные артикулы одинаковой длины.
    if _looks_like_modifier(pieces[-1], pieces[0]):
        pieces = pieces[:-1]
    return list(dict.fromkeys(pieces))


def parse_article(
    value: object,
    modifier_separators: str = DEFAULT_MODIFIER_SEPARATORS,
    options: KeyOptions = DEFAULT_KEY_OPTIONS,
) -> Article:
    """Раскладывает один артикул на базу и модификацию."""
    text = _clean(value)
    if not text:
        return Article(raw="", key="", base_key="")
    key = code_key(text, options)
    if modifier_separators:
        head, modifier = _split_modifier(text, modifier_separators)
        if modifier:
            return Article(
                raw=text,
                key=key,
                base_key=code_key(head, options),
                modifier=modifier,
                quantity=parse_quantity(modifier),
            )
    return Article(raw=text, key=key, base_key=key)


def article_variants(
    value: object,
    separators: str = DEFAULT_SEPARATORS,
    modifier_separators: str = DEFAULT_MODIFIER_SEPARATORS,
    options: KeyOptions = DEFAULT_KEY_OPTIONS,
) -> list[Article]:
    """Все прочтения ячейки: каждый перечисленный артикул и форма «база + модификация».

    ``zrp0050perG32/zrp0010perG32`` даёт два самостоятельных артикула, а
    ``ABC123/50`` — базу ``ABC123`` с модификацией ``50``.
    """
    variants: list[Article] = []
    seen: set[str] = set()
    for part in split_articles(value, separators):
        variant = parse_article(part, modifier_separators, options)
        if variant and variant.key not in seen:
            seen.add(variant.key)
            variants.append(variant)
    whole = parse_article(value, modifier_separators, options)
    if whole.has_modifier and whole.key not in seen:
        variants.append(whole)
    return variants


def base_key(value: object, separators: str = DEFAULT_SEPARATORS,
             modifier_separators: str = DEFAULT_MODIFIER_SEPARATORS,
             options: KeyOptions = DEFAULT_KEY_OPTIONS) -> str:
    """Базовый артикул первого варианта — короткий доступ для поиска и индексов."""
    variants = article_variants(value, separators, modifier_separators, options)
    return variants[0].base_key if variants else ""


def _clean(value: object) -> str:
    """Текст артикула без лишних пробелов. Формулы значением не считаются."""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return "" if text.startswith("=") else text


def _split_modifier(text: str, separators: str) -> tuple[str, str]:
    """Отделяет хвост-модификацию. Если хвост не похож на неё — модификации нет."""
    position = max((text.rfind(ch) for ch in separators), default=-1)
    if position <= 0 or position == len(text) - 1:
        return text, ""
    head, tail = text[:position].strip(), text[position + 1:].strip()
    if not head or not _looks_like_modifier(tail, head):
        return text, ""
    return head, tail


def _looks_like_modifier(tail: str, head: str) -> bool:
    """Хвост — объём, размер или цвет, а не самостоятельный артикул.

    Одной длины хвоста мало: «A-1|B-2» — это два артикула, а не артикул с
    модификацией. Решает соотношение с базой — модификация заметно короче
    того, что она уточняет.
    """
    if not tail or len(tail) > MAX_MODIFIER_LENGTH:
        return False
    if not (any(ch.isdigit() for ch in tail) or len(tail) < MIN_ARTICLE_LENGTH):
        return False
    return len(tail) * 2 <= len(head)
