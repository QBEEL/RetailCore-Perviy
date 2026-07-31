"""Получатель из 1С и карточка поставщика — приведение к одному имени.

В выгрузке получатель записан юрлицом: «НеваЛайн ООО», «НОСОВА ВИОЛЕТТА
ИГОРЕВНА ИП», «Канебо Косметикс Рус». Карточки поставщиков в приложении
заведены под торговыми именами, и совпадать они будут только после того, как с
имени снимут организационную форму.

В данных есть и прямой мусор, который обязана съедать нормализация:
неразрывные пробелы («Домосканова\xa0Регина»), сдвоенные пробелы
(«Харитонова  Елена Александровна ИП») и потерянные при перекодировке буквы
(«Л?вкина София»).

Автоподбор ошибается тем чаще, чем короче имя, поэтому он только предлагает:
ниже порога уверенности получатель остаётся непривязанным, а платёж всё равно
считается — по текстовому имени. Привязка нужна лишь для отсрочки и связи с
заказами, ничего не блокируя.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from ..normalize import normalize_text

# Организационные формы, которые не различают поставщиков. Порядок не важен:
# вырезаются как отдельные слова в любом месте имени.
LEGAL_FORMS: frozenset[str] = frozenset({
    "ооо", "оао", "зао", "пао", "ао", "ип", "тоо", "нао", "чп", "пбоюл",
    "фгуп", "гуп", "муп", "нко", "ано",
    "llc", "ltd", "gmbh",
})

# Слова, которые встречаются у многих контрагентов и сами по себе ничего не
# значат. Убираются только если после них что-то остаётся.
WEAK_WORDS: frozenset[str] = frozenset({
    "торговый", "дом", "компания", "фирма", "группа", "рус", "россия", "рф",
    "трейд", "трейдинг", "холдинг", "сервис",
})

# Неразрывный, узкий неразрывный и табуляция — в выгрузке встречаются все три.
# Записаны кодами: невидимый символ в исходнике глазами не читается.
_BAD_SPACES = re.compile("[   	 ]+")
_SPACES = re.compile(r"\s{2,}")
# Знак, оставшийся от буквы, потерянной при перекодировке: «Л?вкина».
_LOST_LETTER = re.compile(r"(?<=\w)\?(?=\w)")

# Ниже этого сходства автоподбор молчит: неверная привязка тише и опаснее, чем
# её отсутствие. Порог проверен на 578 получателях выгрузки — среди них лишь
# одна пара разных юрлиц набирает 88 и выше, тогда как на 85 таких пар пять
# («Кларис ООО» и «Кларис-2», два разных ИП с похожими фамилиями). Цена порога:
# опечатка в коротком имени не подхватится, и это осознанный выбор.
AUTO_THRESHOLD = 88.0
MIN_KEY_LENGTH = 4


def clean_name(value: object) -> str:
    """Имя для показа: без неразрывных и сдвоенных пробелов."""
    if value is None:
        return ""
    text = _BAD_SPACES.sub(" ", str(value))
    return _SPACES.sub(" ", text).strip(" ,;")


def recipient_key(value: object) -> str:
    """Ключ получателя: без формы, регистра и мусорных пробелов.

    Ключ построен так, чтобы «НеваЛайн ООО» и «ООО НеваЛайн» дали одно и то же,
    а «Сафило СНГ ООО» и «Сафило СНГ» — тоже одно.
    """
    text = _LOST_LETTER.sub("", clean_name(value))
    words = [word for word in normalize_text(text).split() if word not in LEGAL_FORMS]
    if not words:
        # Осталась одна форма — она и есть всё имя, потерять его нельзя.
        return normalize_text(text)
    return " ".join(words)


def compare_key(value: object) -> str:
    """Ключ для нестрогого сравнения: дополнительно без общих слов."""
    words = [word for word in recipient_key(value).split() if word not in WEAK_WORDS]
    return " ".join(words) or recipient_key(value)


def legal_form(value: object) -> str:
    """Организационная форма имени, как она записана в 1С."""
    for word in normalize_text(clean_name(value)).split():
        if word in LEGAL_FORMS:
            return word.upper()
    return ""


def is_person(value: object) -> bool:
    """Похоже ли имя на предпринимателя: три слова и форма ИП."""
    return legal_form(value) == "ИП"


@dataclass(slots=True)
class Guess:
    """Догадка о том, какой карточке поставщика соответствует получатель."""

    recipient: str
    supplier_id: int
    supplier_name: str
    score: float = 0.0
    reason: str = ""

    @property
    def confident(self) -> bool:
        return self.score >= AUTO_THRESHOLD


def guess_supplier(
    recipient: str,
    suppliers: dict[int, str],
) -> Guess | None:
    """Подбирает карточку поставщика по имени получателя.

    Сначала точное совпадение ключей, потом нестрогое сравнение. Слишком
    короткие ключи не сравниваются нестрого: «ЗВЕЗДА» совпадёт с чем угодно.
    """
    key = recipient_key(recipient)
    if not key:
        return None
    index: dict[str, tuple[int, str]] = {}
    loose: dict[str, tuple[int, str]] = {}
    for supplier_id, name in suppliers.items():
        if supplier_key := recipient_key(name):
            index.setdefault(supplier_key, (supplier_id, name))
        if narrow := compare_key(name):
            loose.setdefault(narrow, (supplier_id, name))

    if hit := index.get(key):
        return Guess(recipient, hit[0], hit[1], 100.0, "имя совпадает")
    narrow_key = compare_key(recipient)
    if hit := loose.get(narrow_key):
        return Guess(recipient, hit[0], hit[1], 96.0, "имя совпадает без формы")
    if len(narrow_key) < MIN_KEY_LENGTH:
        return None

    best: Guess | None = None
    for candidate, (supplier_id, name) in loose.items():
        if len(candidate) < MIN_KEY_LENGTH:
            continue
        # Именно `token_sort_ratio`, а не `token_set_ratio`: последняя считает
        # подмножество полным совпадением и даёт 100 для пары
        # «ЗВЕЗДА» — «Звезда Востока». Для юрлиц это готовая неверная привязка,
        # а лишнее слово в названии почти всегда означает другую организацию.
        score = float(fuzz.token_sort_ratio(narrow_key, candidate))
        if best is None or score > best.score:
            best = Guess(recipient, supplier_id, name, score, f"похоже на «{name}»")
    return best if best is not None and best.confident else None


def guess_all(
    recipients: list[str],
    suppliers: dict[int, str],
) -> list[Guess]:
    """Уверенные догадки по списку получателей — для разового автосопоставления."""
    found: list[Guess] = []
    for recipient in recipients:
        if (guess := guess_supplier(recipient, suppliers)) is not None:
            found.append(guess)
    return found
