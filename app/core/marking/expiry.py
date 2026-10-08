"""Срок годности из содержимого QR: что склад видит, пикнув товар.

На товаре стоит QR, в котором лежит карточка кода из ГИС МТ: название, бренд,
дата производства, статус и — главное — «Дата срока годности». Карточка бывает
двух видов, и приходится понимать оба:

* текст «Ключ: значение» по строке на поле (так выглядит QR с образца), причём
  часть полей стоит в одной строке через запятую:
  `…Признак выбытия от не владельца:false,Дата срока годности: 2029-01-31…`.
  Поэтому разбирать по строкам нельзя — поле ищется по подписи;
* настоящий JSON с теми же полями.

В карточке десяток дат — нанесения, ввода в оборот, производства, эмиссии,
документа, срока годности. Брать «последнюю» или «самую позднюю» нельзя: срок
выбирается только по подписи. Не нашлась подпись — срок остаётся пустым, и
программа говорит почему: показать склад выдуманную дату хуже, чем честно
сказать «не разобрала».

Дата берётся из строки как есть. `2029-01-31T00:00:00.000Z` — это полночь по
Гринвичу, и перевод в местное время сдвинул бы её на сутки там, где часовой пояс
отрицательный. Срок годности — календарный день, а не момент.

У парфюмерии срока в ответе системы нет вовсе — только дата производства, а срок
у неё один: три года, у отдельных брендов пять. Тогда срок считается от даты
производства (`apply_estimate`) и везде показывается как расчётный: это не данные
системы, а правило, и склад должен видеть разницу. Правило — по умолчанию три
года и список брендов с другим сроком — задаётся в настройках вкладки.

Разбор ничего не бросает: непонятное складывается в `problems`.
"""
from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from . import codes as codes_module

# Подписи срока годности в текстовой карточке. «Годен до» — на случай этикеток,
# где поле названо по-человечески.
_EXPIRY_LABEL = re.compile(
    r"(?:дата\s+срока\s+годности|срок\s+годности|годен\s+до)\s*[:=]\s*[\"']?\s*"
    r"(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})",
    re.IGNORECASE)
_PRODUCED_LABEL = re.compile(
    r"дата\s+производства\s*[:=]\s*[\"']?\s*(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})",
    re.IGNORECASE)
_ANY_DATE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")

# Ключи JSON, приведённые к виду «строчные буквы без пробелов и знаков».
_JSON_EXPIRY = frozenset({
    "срокгодности", "датасрокагодности", "expirationdate", "expirydate",
    "expireddate", "expiredate", "bestbefore", "validuntil",
})
_JSON_PRODUCED = frozenset({"датапроизводства", "productiondate", "producedat", "produceddate"})
_JSON_NAME = frozenset({"наименование", "name", "productname"})
_JSON_BRAND = frozenset({"бренд", "brand"})
_JSON_GTIN = frozenset({"кодтовара", "gtin"})
_JSON_KI = frozenset({"киз", "cis", "ki", "requestedcis"})
_JSON_STATUS = frozenset({"статус", "status"})
_JSON_OWNER = frozenset({"ownername", "владелец"})
_JSON_MAKER = frozenset({"manufacturername", "producername"})


class ExpiryLevel(Enum):
    """Что делать с товаром, у которого такой срок."""

    OK = ("ok", "В порядке")
    SOON = ("soon", "Скоро истекает")
    EXPIRED = ("expired", "Просрочен")
    UNKNOWN = ("unknown", "Срок не найден")

    def __init__(self, value: str, title: str) -> None:
        self._value_ = value
        self.title = title


@dataclass(slots=True)
class Card:
    """Карточка из QR: то, что нужно складу, и то, что не удалось прочитать."""

    raw: str = ""
    expires: date | None = None
    produced: date | None = None
    name: str = ""
    brand: str = ""
    gtin: str = ""
    kiz: str = ""
    status: str = ""
    owner: str = ""
    manufacturer: str = ""
    # Это голый код маркировки (DataMatrix с упаковки), а не карточка: срока в нём
    # нет, и узнать его можно только у системы маркировки.
    bare: bool = False
    # Срок посчитан от даты производства, а не прочитан. `shelf_years` — на сколько
    # лет считали: в подсказке видно, откуда взялась дата.
    estimated: bool = False
    shelf_years: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def serial(self) -> str:
        return codes_module.parse(self.kiz).serial if self.kiz else ""

    @property
    def key(self) -> str:
        """Что делает экземпляр единственным: код идентификации, иначе весь текст.

        Нужен для повторов: один и тот же товар сканируют дважды чаще, чем
        хотелось бы, и во второй раз об этом надо сказать.
        """
        if self.kiz and (ki := codes_module.parse(self.kiz).ki):
            return ki
        return self.kiz or " ".join(self.raw.split())

    @property
    def title(self) -> str:
        return self.name or self.gtin or "без названия"


BOM = chr(0xFEFF)  # метка порядка байт в начале текста


def _clean(text: str) -> str:
    """BOM, префикс символики сканера (`]Q1`) и переводы строк — в обычный вид."""
    value = (text or "").replace(BOM, "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if re.match(r"^\][A-Za-z][0-9A-Za-z]", value):
        value = value[3:].lstrip()
    return value


def _to_date(value: str) -> date | None:
    """`2029-01-31T00:00:00.000Z`, `2029-01-31` и `31.01.2029` → дата."""
    text = (value or "").strip()
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text[:10])
        if re.match(r"^\d{2}\.\d{2}\.\d{4}", text):
            return datetime.strptime(text[:10], "%d.%m.%Y").date()
    except ValueError:
        return None
    return None


def _from_ai17(value: str) -> date | None:
    """AI 17 — «годен до» в GS1: ГГММДД, а день «00» означает конец месяца."""
    if not re.fullmatch(r"\d{6}", value or ""):
        return None
    year, month, day = 2000 + int(value[:2]), int(value[2:4]), int(value[4:])
    try:
        if day == 0:
            day = calendar.monthrange(year, month)[1]
        return date(year, month, day)
    except ValueError:
        return None


def _unquote(value: str) -> str:
    """Кавычки вокруг всего значения снимаются, внутри названия — остаются:
    `ООО "БИГДИЛС"` кончается кавычкой, но она часть названия."""
    if len(value) > 1 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _from_ai11(value: str) -> date | None:
    """AI 11 — дата производства в GS1: ГГММДД, день «00» — первое число месяца."""
    if not re.fullmatch(r"\d{6}", value or ""):
        return None
    year, month, day = 2000 + int(value[:2]), int(value[2:4]), int(value[4:]) or 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _line_value(text: str, *labels: str) -> str:
    """Значение поля «Подпись: значение» — до конца строки.

    Подпись привязана к началу строки: иначе «Наименование» зацепилось бы за
    «Наименование ТГ» из середины соседней строки.
    """
    for label in labels:
        pattern = re.compile(
            rf"^[ \t]*{label}[ \t]*:[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
        if (found := pattern.search(text)) and found.group(1).strip():
            return _unquote(found.group(1).strip())
    return ""


def _walk(node: object):
    """Все пары «ключ → значение» JSON, на любой глубине."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _normal_key(key: object) -> str:
    return re.sub(r"[\s_\-:]+", "", str(key)).lower()


def _from_json(text: str, card: Card) -> bool:
    """Заполняет карточку из JSON. Возвращает, получилось ли прочитать как JSON."""
    try:
        data = json.loads(text)
    except ValueError:
        return False
    pairs = [(_normal_key(key), value) for key, value in _walk(data)]

    def first(names: frozenset[str]) -> object:
        return next((v for k, v in pairs if k in names and isinstance(v, (str, int))), "")

    card.expires = _to_date(str(first(_JSON_EXPIRY)))
    card.produced = _to_date(str(first(_JSON_PRODUCED)))
    card.name = str(first(_JSON_NAME)).strip()
    card.brand = str(first(_JSON_BRAND)).strip()
    card.gtin = str(first(_JSON_GTIN)).strip()
    card.kiz = str(first(_JSON_KI)).strip()
    card.status = str(first(_JSON_STATUS)).strip()
    card.owner = str(first(_JSON_OWNER)).strip()
    card.manufacturer = str(first(_JSON_MAKER)).strip()
    return True


def _from_text(text: str, card: Card) -> None:
    """Заполняет карточку из текста «Ключ: значение»."""
    if found := _EXPIRY_LABEL.search(text):
        card.expires = _to_date(found.group(1))
    if found := _PRODUCED_LABEL.search(text):
        card.produced = _to_date(found.group(1))
    card.name = _line_value(text, "Наименование")
    card.brand = _line_value(text, "Бренд")
    card.gtin = _line_value(text, "Код товара")
    card.kiz = _line_value(text, "КиЗ", "КИ")
    card.status = _line_value(text, "Статус")
    card.owner = _line_value(text, "Наименование владельца товара")
    card.manufacturer = _line_value(text, "Наименование производителя")


def parse_card(text: str) -> Card:
    """Разбирает содержимое QR. Не бросает: непонятное оказывается в `problems`."""
    value = _clean(text)
    card = Card(raw=value)
    if not value:
        card.problems.append("пустая строка")
        return card

    as_json = value.startswith(("{", "[")) and _from_json(value, card)
    if not as_json:
        _from_text(value, card)

    # Голый код маркировки без карточки: срок — в поле AI 17, если оно есть.
    bare = codes_module.parse(value) if "\n" not in value and value.startswith("01") else None
    if bare is not None and bare.valid:
        card.bare = card.kiz == "" and not card.name
        card.kiz = card.kiz or codes_module.from_keyboard(value)
        card.gtin = card.gtin or bare.gtin
        if card.expires is None:
            card.expires = _from_ai17(bare.fields.get("17", ""))
        if card.produced is None:
            card.produced = _from_ai11(bare.fields.get("11", ""))

    if card.kiz:
        code = codes_module.parse(card.kiz)
        card.gtin = card.gtin or code.gtin

    if card.expires is None:
        if _ANY_DATE.search(value):
            card.problems.append(
                "В тексте есть даты, но «Дата срока годности» не найдена. Если сканер "
                "не передаёт русские буквы, подпись приходит искажённой — проверьте "
                "настройки сканера (раскладка, режим вывода).")
        else:
            card.problems.append("В этом QR нет срока годности")
    return card


def from_response(kiz: str, answer: dict) -> Card:
    """Карточка из ответа ГИС МТ по коду — когда срока в самом коде нет.

    Ответ — тот же набор полей, что в QR-карточке, только вложенным JSON
    (`cisInfo`), поэтому читается тем же разбором по ключам. Код остаётся
    тем, что отсканировали: ответ мог вернуть его в другом виде, а ключ повтора
    должен быть один на экземпляр.
    """
    card = Card(raw=kiz, kiz=kiz)
    _from_json(json.dumps(answer, ensure_ascii=False, default=str), card)
    card.kiz = kiz
    code = codes_module.parse(kiz)
    card.gtin = card.gtin or code.gtin
    if card.expires is None:
        names = sorted({str(key) for key, _ in _walk(answer)})
        card.problems.append(
            "В ответе «Честного ЗНАКа» нет срока годности по этому коду"
            + (f" (поля ответа: {', '.join(names[:30])})" if names else ""))
    return card


# Сообщения «срока нет» — они снимаются, когда срок посчитан по дате производства.
_MISSING = ("В тексте есть даты", "В этом QR нет срока годности",
            "В ответе «Честного ЗНАКа» нет срока годности")

DEFAULT_SHELF_YEARS = 3


def brand_key(brand: str) -> str:
    """Бренд для сравнения: регистр, ё, кавычки и лишние пробелы не различают."""
    text = (brand or "").casefold().replace("ё", "е")
    return " ".join(re.sub(r"[\"'«»“”]", " ", text).split())


def shelf_years(brand: str, default: int = DEFAULT_SHELF_YEARS,
                by_brand: dict[str, int] | None = None) -> int:
    """На сколько лет считать срок: по бренду, если он в списке, иначе по умолчанию."""
    key = brand_key(brand)
    if key:
        for name, years in (by_brand or {}).items():
            if brand_key(name) == key and years > 0:
                return int(years)
    return int(default) if default > 0 else DEFAULT_SHELF_YEARS


def add_years(day: date, years: int) -> date:
    """Та же дата через `years` лет; 29 февраля в невисокосном году — 28-е."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def estimate_expiry(produced: date, years: int) -> date:
    """Последний день срока: дата производства плюс срок, минус сутки.

    Сутки вычитаются, потому что срок в три года, начатый 20.01.2026, кончается
    19.01.2029 — так и стоит в карточке настоящего товара. Лучше показать склад
    на день раньше, чем на день позже.
    """
    from datetime import timedelta
    return add_years(produced, years) - timedelta(days=1)


def apply_estimate(card: Card, default: int = DEFAULT_SHELF_YEARS,
                   by_brand: dict[str, int] | None = None) -> bool:
    """Считает срок по дате производства, если в карточке его нет.

    Прочитанный срок не трогается никогда: расчёт — запасной путь для парфюмерии,
    а не поправка к данным. Возвращает, посчитан ли срок.
    """
    if card.expires is not None or card.produced is None:
        return False
    years = shelf_years(card.brand, default, by_brand)
    card.expires = estimate_expiry(card.produced, years)
    card.estimated = True
    card.shelf_years = years
    card.problems = [p for p in card.problems if not p.startswith(_MISSING)]
    return True


def clear_estimate(card: Card) -> None:
    """Снимает расчётный срок — перед пересчётом по новым правилам."""
    if card.estimated:
        card.expires, card.estimated, card.shelf_years = None, False, 0


def parse_brand_years(text: str) -> tuple[dict[str, int], list[str]]:
    """Строки «Бренд = 5» → словарь и список ошибок по строкам.

    Пустые строки пропускаются. Допустимы «=», «:» и табуляция: список проще
    вставить из таблицы, чем набирать.
    """
    result: dict[str, int] = {}
    errors: list[str] = []
    for number, line in enumerate((text or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^(.+?)\s*[=:\t]\s*(\d{1,2})\s*(?:л\.?|лет|года?|г\.?)?$", line,
                         re.IGNORECASE)
        if not match or not 1 <= int(match.group(2)) <= 30:
            errors.append(f"строка {number}: нужно «Бренд = число лет», а здесь «{line}»")
            continue
        result[match.group(1).strip()] = int(match.group(2))
    return result, errors


def format_brand_years(by_brand: dict[str, int]) -> str:
    return "\n".join(f"{name} = {years}" for name, years in by_brand.items())


def days_left(expires: date | None, today: date | None = None) -> int | None:
    """Сколько дней до конца срока; отрицательное число — столько дней просрочено."""
    if expires is None:
        return None
    return (expires - (today or date.today())).days


def level_of(expires: date | None, soon_days: int, today: date | None = None) -> ExpiryLevel:
    """Просрочен — раньше сегодняшнего дня. В последний день срока товар годен."""
    left = days_left(expires, today)
    if left is None:
        return ExpiryLevel.UNKNOWN
    if left < 0:
        return ExpiryLevel.EXPIRED
    if left <= soon_days:
        return ExpiryLevel.SOON
    return ExpiryLevel.OK
