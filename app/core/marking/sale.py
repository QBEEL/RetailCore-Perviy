"""Продажа маркированного товара организации для её собственных нужд: УПД с выводом из оборота.

Когда организация покупает товар не для перепродажи (парфюм в подарок
сотрудникам, в офис, для своих нужд), коды маркировки должны выбыть из оборота
— дальше их никто не продаёт. Делать это отдельным документом «Вывод из
оборота» не нужно: ЦРПТ предусмотрел признак прямо в УПД. Продавец пишет в
«Информацию продавца»

    <ИнфПолФХЖ1><ТекстИнф Идентиф="СвВыбытияМАРК" Значен="1"/></ИнфПолФХЖ1>

и, когда покупатель подписывает документ, ГИС МТ одним действием переводит коды
на покупателя и выводит их из оборота. Без признака коды просто перешли бы на
покупателя и повисли на нём «в обороте». Значения признака — из «Методических
рекомендаций по описанию сведений о передаче маркированных товаров» ЦРПТ
(версия 30, раздел 4.2): 1 — покупка для собственных нужд, не связанных с
последующей продажей; 3 — для производственных целей покупателя. Регистрироваться
в «Честном ЗНАКе» покупателю при этом не нужно (там же, раздел 2.10).

Формат — УПД по приказу ФНС ЕД-7-26/970@ (версия 5.03), функция СЧФДОП: и
счёт-фактура, и передаточный документ. Состав сверен со схемой
`ON_NSCHFDOPPR_1_997_01_05_03_05.xsd` и с настоящим УПД 5.03 из Диадока. Коды
идут в `ДопСведТов/НомСредИдентТов/КИЗ` по одному — кодом идентификации, без
криптохвоста (раздел 2.2 рекомендаций); спецсимволы кода экранируются.

Три вещи в формате неочевидны и потому записаны здесь:

- **Адресат — в имени файла.** Блока `СвУчДокОбор` в 5.03 нет: идентификаторы
  ЭДО получателя и отправителя стоят в `ИдФайл`, а имя файла обязано с ним
  совпадать. Порядок — сначала получатель, потом отправитель, как в образце.
- **Кодировка — windows-1251.** Так требует формат. Знаки, которых в ней нет
  (é в «Chloé»), пишутся ссылкой `&#233;` — это тот же XML, а не порча названия.
- **Подписывается файл, а не текст.** Подпись считается по байтам windows-1251,
  ровно тем, что уходят оператору; см. `crypto.sign`.

Деньги считаются в `Decimal` с округлением до копеек «от половины вверх»: цена
вводится с НДС, налог выделяется из суммы строки, а цена без НДС — из остатка.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from xml.sax.saxutils import escape, quoteattr

from ... import __version__
from . import codes as codes_module
from .introduce import MAX_PRODUCTS

# Причины выбытия, которые продавец указывает сам («Информация продавца»).
# Значение 2 (безвозмездная передача) здесь не предлагается: это не продажа.
WITHDRAWAL_REASONS: dict[str, str] = {
    "1": "Покупатель берёт для собственных нужд, не для перепродажи",
    "3": "Покупатель использует в производстве, не для перепродажи",
}
DEFAULT_REASON = "1"

# Ставки — те, что принимает схема, кроме «расчётных» (22/122 и подобных): они
# для авансов и налоговых агентов. Ставки по умолчанию нет намеренно: ошибка в
# ней даёт неверный счёт-фактуру, и выбрать её должен человек.
VAT_RATES: tuple[str, ...] = ("22%", "10%", "7%", "5%", "0%", "без НДС")
NO_VAT = "без НДС"

FORMAT_VERSION = "5.03"
KND = "1115131"
FUNCTION = "СЧФДОП"
FACT = ("Документ об отгрузке товаров (выполнении работ), передаче имущественных "
        "прав (документ об оказании услуг)")
DOCUMENT_NAME = "Универсальный передаточный документ"
OPERATION = "Товары переданы"
FILE_PREFIX = "ON_NSCHFDOPPR"
# Хвост имени файла N2…N7: третий признак — «в документе есть маркированный
# товар». Так у УПД с кодами из Диадока и у других генераторов 5.03. В УПД
# только на товар без марок признак снят.
FILE_FLAGS = "0_1_0_0_0_00"
FILE_FLAGS_UNMARKED = "0_0_0_0_0_00"

# Где хранится машиночитаемая доверенность. Для МЧД, зарегистрированной в
# распределённом реестре ФНС, это его адрес.
POA_SYSTEM = "https://m4d.nalog.gov.ru/"

# Штука по ОКЕИ — маркированный товар продаётся поштучно.
UNIT_CODE = "796"
UNIT_NAME = "шт"

_INN = re.compile(r"^(\d{10}|\d{12})$")
_KPP = re.compile(r"^\d{9}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Идентификатор участника ЭДО: код оператора из трёх знаков и номер абонента —
# «2BM-…» у Диадока, «2LT-…» у «ЭДО Лайт», а у СБИС без дефиса: «2BE» и 32
# шестнадцатеричных знака.
_EDO_ID = re.compile(r"^[0-9A-Za-z]{3}[0-9A-Za-z\-]{1,97}$")
_CENT = Decimal("0.01")

# Страны происхождения — коды ОКСМ и краткие названия, как их пишут в УПД.
# Список не полный: в нём то, откуда приходит парфюмерия и косметика. Страну не
# из списка можно вписать названием — тогда код в документ не попадёт.
COUNTRIES: dict[str, str] = {
    "250": "ФРАНЦИЯ", "380": "ИТАЛИЯ", "276": "ГЕРМАНИЯ", "724": "ИСПАНИЯ",
    "826": "СОЕДИНЕННОЕ КОРОЛЕВСТВО", "840": "СОЕДИНЕННЫЕ ШТАТЫ",
    "784": "ОБЪЕДИНЕННЫЕ АРАБСКИЕ ЭМИРАТЫ", "756": "ШВЕЙЦАРИЯ",
    "528": "НИДЕРЛАНДЫ", "056": "БЕЛЬГИЯ", "040": "АВСТРИЯ", "620": "ПОРТУГАЛИЯ",
    "300": "ГРЕЦИЯ", "616": "ПОЛЬША", "203": "ЧЕХИЯ", "703": "СЛОВАКИЯ",
    "348": "ВЕНГРИЯ", "642": "РУМЫНИЯ", "100": "БОЛГАРИЯ", "752": "ШВЕЦИЯ",
    "208": "ДАНИЯ", "246": "ФИНЛЯНДИЯ", "578": "НОРВЕГИЯ", "372": "ИРЛАНДИЯ",
    "792": "ТУРЦИЯ", "376": "ИЗРАИЛЬ", "682": "САУДОВСКАЯ АРАВИЯ",
    "156": "КИТАЙ", "410": "КОРЕЯ, РЕСПУБЛИКА", "392": "ЯПОНИЯ", "356": "ИНДИЯ",
    "764": "ТАИЛАНД", "704": "ВЬЕТНАМ", "124": "КАНАДА", "036": "АВСТРАЛИЯ",
    "076": "БРАЗИЛИЯ", "643": "РОССИЯ", "112": "БЕЛАРУСЬ", "398": "КАЗАХСТАН",
    "051": "АРМЕНИЯ", "417": "КИРГИЗИЯ", "860": "УЗБЕКИСТАН",
}
# Названия, которыми страну называют чаще, чем по ОКСМ.
_COUNTRY_ALIASES: dict[str, str] = {
    "ВЕЛИКОБРИТАНИЯ": "826", "АНГЛИЯ": "826", "США": "840", "ОАЭ": "784",
    "КОРЕЯ": "410", "ЮЖНАЯ КОРЕЯ": "410", "ГОЛЛАНДИЯ": "528",
}


class SaleProblem(ValueError):
    """Документ собрать нельзя. Текст пригоден для показа."""


def country(text: str) -> tuple[str, str]:
    """Страна происхождения из того, что вписал человек: код ОКСМ и название.

    Принимается и код («250»), и название в любом регистре («Франция»). Код без
    названия не годится — схема требует назвать страну, если указан её код, — а
    название без кода годится: так пишется страна не из справочника.
    """
    value = " ".join((text or "").split())
    if not value:
        return "", ""
    if value.isdigit():
        code = value.zfill(3)
        return code, COUNTRIES.get(code, "")
    upper = value.upper().replace("Ё", "Е")
    code = _COUNTRY_ALIASES.get(upper) or next(
        (key for key, name in COUNTRIES.items() if name == upper), "")
    return code, COUNTRIES.get(code, value.upper())


def money(value: object) -> Decimal | None:
    """Сумма из того, что вписал человек: «1 250,50» → 1250.50. Мусор — None."""
    text = str(value or "").replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    return amount.quantize(_CENT, ROUND_HALF_UP)


def _rate(vat: str) -> Decimal | None:
    """Процент ставки; None — «без НДС»."""
    if vat == NO_VAT:
        return None
    return Decimal(vat.rstrip("%").replace(",", "."))


@dataclass(frozen=True, slots=True)
class Amounts:
    """Сумма без налога, налог и сумма с налогом."""

    net: Decimal = Decimal("0.00")
    vat: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")

    def __add__(self, other: "Amounts") -> "Amounts":
        return Amounts(self.net + other.net, self.vat + other.vat,
                       self.total + other.total)


def amounts(price: Decimal, quantity: int, vat: str) -> Amounts:
    """Стоимость строки по цене за штуку с НДС.

    Налог выделяется из суммы строки, а не из цены: иначе на сотне флаконов
    копейки округления сложились бы в рубли, и итог разошёлся бы с ценой × число.
    """
    total = (price * quantity).quantize(_CENT, ROUND_HALF_UP)
    rate = _rate(vat)
    if rate is None:
        return Amounts(total, Decimal("0.00"), total)
    tax = (total * rate / (100 + rate)).quantize(_CENT, ROUND_HALF_UP)
    return Amounts(total - tax, tax, total)


@dataclass(frozen=True, slots=True)
class Party:
    """Продавец или покупатель.

    Организацию от предпринимателя отличает ИНН: у организации 10 цифр, у ИП —
    12. Для ИП в `name` пишется ФИО («Саух Мария Николаевна», можно с «ИП»
    впереди): в формате это фамилия, имя и отчество по отдельности, а не
    название.
    """

    inn: str = ""
    name: str = ""
    kpp: str = ""
    address: str = ""
    edo_id: str = ""

    @property
    def person(self) -> bool:
        return len(self.inn.strip()) == 12

    @property
    def fio(self) -> tuple[str, str, str]:
        words = self.name.split()
        for prefix in (("индивидуальный", "предприниматель"), ("ип",)):
            if [word.lower().strip(".") for word in words[:len(prefix)]] == list(prefix):
                words = words[len(prefix):]
                break
        return (words[0] if words else "", words[1] if len(words) > 1 else "",
                " ".join(words[2:]))

    @property
    def title(self) -> str:
        if self.person:
            return "ИП " + " ".join(part for part in self.fio if part)
        return " ".join(self.name.split())

    def problems(self, role: str) -> list[str]:
        found: list[str] = []
        inn = self.inn.strip()
        if not _INN.match(inn):
            found.append(f"ИНН {role} — 10 цифр для организации, 12 для ИП")
        if self.person:
            surname, name, _ = self.fio
            if not surname or not name:
                found.append(f"для ИП-{role} нужны фамилия и имя (ФИО вместо названия)")
        elif not self.name.strip():
            found.append(f"не указано название {role}")
        if inn and not self.person and not _KPP.match(self.kpp.strip()):
            found.append(f"КПП {role} — 9 цифр")
        if not self.address.strip():
            found.append(f"не указан адрес {role}")
        if not _EDO_ID.match(self.edo_id.strip()):
            found.append(f"идентификатор ЭДО {role} — вида «2LT-…», «2BM-…» или «2BE…»")
        return found

    def xml(self, tag: str, indent: str) -> list[str]:
        """Узел участника: идентификация и адрес."""
        inner = indent + "\t"
        lines = [f"{indent}<{tag}>", f"{inner}<ИдСв>"]
        if self.person:
            surname, name, patronymic = self.fio
            lines.append(f"{inner}\t<СвИП ИННФЛ={_attr(self.inn)}>")
            lines.append(f"{inner}\t\t{_fio(surname, name, patronymic)}")
            lines.append(f"{inner}\t</СвИП>")
        else:
            kpp = f" КПП={_attr(self.kpp)}" if self.kpp.strip() else ""
            lines.append(f"{inner}\t<СвЮЛУч НаимОрг={_attr(self.name)} "
                         f"ИННЮЛ={_attr(self.inn)}{kpp}/>")
        lines.append(f"{inner}</ИдСв>")
        lines.append(f"{inner}<Адрес>")
        lines.append(f'{inner}\t<АдрИнф КодСтр="643" НаимСтран="Россия" '
                     f"АдрТекст={_attr(self.address)}/>")
        lines.append(f"{inner}</Адрес>")
        lines.append(f"{indent}</{tag}>")
        return lines


@dataclass(frozen=True, slots=True)
class Signer:
    """Кто подписывает документ.

    ФИО должно совпасть с сертификатом, которым подписывают. Подписывает не
    руководитель (не сам ИП) — нужна машиночитаемая доверенность: её номер и
    дата попадают в документ, и способ подтверждения полномочий становится «по
    доверенности» (3) вместо «по данным подписи» (1).
    """

    surname: str = ""
    name: str = ""
    patronymic: str = ""
    position: str = ""
    poa_number: str = ""
    poa_date: str = ""
    poa_system: str = POA_SYSTEM

    @property
    def by_poa(self) -> bool:
        return bool(self.poa_number.strip())

    @property
    def title(self) -> str:
        return " ".join(part for part in (self.surname, self.name, self.patronymic)
                        if part.strip())

    @property
    def problems(self) -> list[str]:
        found: list[str] = []
        if not self.surname.strip() or not self.name.strip():
            found.append("у подписанта нужны фамилия и имя")
        # По схеме ФНС должность необязательна, но «ЭДО Лайт» для СЧФДОП её
        # требует: «Элемент Документ.Подписант.Должн обязателен при
        # <Функция>=СЧФДОП | ДОП» — ответ боевого контура.
        if not self.position.strip():
            found.append("у подписанта нужна должность (ИП — «Индивидуальный "
                         "предприниматель»)")
        if self.by_poa:
            if not _GUID.match(self.poa_number.strip()):
                found.append("номер МЧД — 36 знаков вида "
                             "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
            if not _DATE.match(self.poa_date.strip()):
                found.append("дата выдачи МЧД — в виде ГГГГ-ММ-ДД")
            if not self.poa_system.strip():
                found.append("не указано, где хранится МЧД")
        return found

    def xml(self, indent: str) -> list[str]:
        position = f" Должн={_attr(self.position)}" if self.position.strip() else ""
        method = "3" if self.by_poa else "1"
        lines = [f'{indent}<Подписант{position} ТипПодпис="1" СпосПодтПолном="{method}">',
                 f"{indent}\t{_fio(self.surname, self.name, self.patronymic)}"]
        if self.by_poa:
            lines.append(f"{indent}\t<СвДоверЭл НомДовер={_attr(self.poa_number)} "
                         f"ДатаВыдДовер=\"{_ru_date(self.poa_date)}\" "
                         f"ИдСистХран={_attr(self.poa_system)}/>")
        lines.append(f"{indent}</Подписант>")
        return lines


@dataclass(frozen=True, slots=True)
class Line:
    """Строка документа: один товар (GTIN) и все его экземпляры.

    У маркированного товара количество — это число кодов: продать четыре
    флакона по трём кодам нельзя, и вписывать количество отдельно значило бы
    однажды разойтись с ним. Товар без марок — например, выпущенный до начала
    обязательной маркировки — продаётся строкой без кодов, и количество у неё
    своё, `count`. Такая строка в ГИС МТ не попадает и ничего не выводит.
    """

    gtin: str
    name: str
    price: Decimal | None
    codes: tuple[str, ...]
    origin: str = ""
    customs: str = ""
    count: int = 0

    @property
    def marked(self) -> bool:
        return bool(self.codes)

    @property
    def quantity(self) -> int:
        return len(self.codes) if self.codes else self.count

    @property
    def identifiers(self) -> list[str]:
        return [codes_module.parse(code).ki or code for code in self.codes]

    def amounts(self, vat: str) -> Amounts:
        return amounts(self.price or Decimal("0"), self.quantity, vat)

    def problems(self, number: int) -> list[str]:
        label = self.name.strip() or self.gtin
        where = f"строка {number}" + (f" ({label})" if label else "")
        found: list[str] = []
        if not self.name.strip():
            found.append(f"{where}: нет наименования товара")
        if self.price is None or self.price <= 0:
            found.append(f"{where}: цена за штуку должна быть больше нуля")
        if not self.codes and self.count <= 0:
            found.append(f"{where}: количество должно быть больше нуля")
        code, name = country(self.origin)
        if code and not name:
            found.append(f"{where}: код страны {code} программе не знаком — "
                         "впишите название страны")
        if len(self.customs.strip()) > 29:
            found.append(f"{where}: номер декларации на товары длиннее 29 знаков")
        return found

    def xml(self, number: int, vat: str, indent: str) -> list[str]:
        cost = self.amounts(vat)
        unit = (cost.net / self.quantity).quantize(_CENT, ROUND_HALF_UP) \
            if self.quantity else Decimal("0.00")
        inner = indent + "\t"
        lines = [f'{indent}<СведТов НомСтр="{number}" НаимТов={_attr(self.name)} '
                 f'ОКЕИ_Тов="{UNIT_CODE}" НаимЕдИзм="{UNIT_NAME}" '
                 f'КолТов="{self.quantity}" ЦенаТов="{unit}" '
                 f'СтТовБезНДС="{cost.net}" НалСт={_attr(vat)} '
                 f'СтТовУчНал="{cost.total}">']
        code, name = country(self.origin)
        customs = self.customs.strip()
        if code or customs:
            parts = []
            if code:
                parts.append(f'КодПроисх="{code}"')
            if customs:
                parts.append(f"НомерДТ={_attr(customs)}")
            lines.append(f"{inner}<СвДТ {' '.join(parts)}/>")
        # GTIN без кодов не пишется: товар без марок не должен выглядеть
        # маркированным, у которого коды забыли.
        gtin = f" ГТИН={_attr(self.gtin)}" if self.marked and self.gtin else ""
        if not name and not self.marked:
            lines.append(f'{inner}<ДопСведТов ПрТовРаб="1"{gtin}/>')
        else:
            lines.append(f'{inner}<ДопСведТов ПрТовРаб="1"{gtin}>')
            if name:
                lines.append(f"{inner}\t<КрНаимСтрПр>{escape(name)}</КрНаимСтрПр>")
            if self.marked:
                lines.append(f"{inner}\t<НомСредИдентТов>")
                for identifier in self.identifiers:
                    lines.append(f"{inner}\t\t<КИЗ>{_text(identifier)}</КИЗ>")
                lines.append(f"{inner}\t</НомСредИдентТов>")
            lines.append(f"{inner}</ДопСведТов>")
        lines.append(f"{inner}<Акциз>")
        lines.append(f"{inner}\t<БезАкциз>без акциза</БезАкциз>")
        lines.append(f"{inner}</Акциз>")
        lines.extend(_tax("СумНал", cost.vat, vat, inner))
        lines.append(f"{indent}</СведТов>")
        return lines


@dataclass(frozen=True, slots=True)
class Sale:
    """УПД продавца на продажу с выводом кодов из оборота.

    `guid` и `created` задаются один раз: из них складывается имя файла и время
    формирования. Тот же документ, собранный дважды, даёт те же байты — подпись,
    посчитанная по первому, подходит ко второму, а «ЭДО Лайт» по тому же имени
    файла узнаёт уже загруженный документ и второго не создаёт.
    """

    number: str
    date: str
    seller: Party
    buyer: Party
    signer: Signer
    lines: tuple[Line, ...]
    vat: str = ""
    reason: str = DEFAULT_REASON
    basis_name: str = ""
    basis_number: str = ""
    basis_date: str = ""
    guid: str = field(default_factory=lambda: str(uuid.uuid4()))
    created: datetime = field(default_factory=lambda: datetime.now().replace(microsecond=0))

    kind = "sale"
    title = "УПД продажи с выводом из оборота"

    @property
    def codes(self) -> list[str]:
        return [code for line in self.lines for code in line.codes]

    @property
    def identifiers(self) -> list[str]:
        return [item for line in self.lines for item in line.identifiers]

    @property
    def quantity(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def marked(self) -> bool:
        """Есть ли в документе коды — а с ними и вывод из оборота."""
        return any(line.marked for line in self.lines)

    @property
    def totals(self) -> Amounts:
        result = Amounts()
        for line in self.lines:
            result = result + line.amounts(self.vat)
        return result

    @property
    def problems(self) -> list[str]:
        found: list[str] = []
        if not self.number.strip():
            found.append("не указан номер документа")
        if not _DATE.match(self.date.strip()):
            found.append("дата документа — в виде ГГГГ-ММ-ДД")
        if self.vat not in VAT_RATES:
            found.append("не выбрана ставка НДС")
        if self.marked and self.reason not in WITHDRAWAL_REASONS:
            found.append("не выбрана причина вывода из оборота")
        found.extend(self.seller.problems("продавца"))
        found.extend(self.buyer.problems("покупателя"))
        if self.seller.inn.strip() and self.seller.inn.strip() == self.buyer.inn.strip():
            found.append("продавец и покупатель — одна и та же организация")
        found.extend(self.signer.problems)
        basis = [self.basis_name, self.basis_number, self.basis_date]
        if any(item.strip() for item in basis) and not all(item.strip() for item in basis):
            found.append("основание (договор) заполняется целиком: наименование, "
                         "номер и дата")
        if self.basis_date.strip() and not _DATE.match(self.basis_date.strip()):
            found.append("дата основания — в виде ГГГГ-ММ-ДД")
        if not self.lines:
            found.append("нет ни одного товара")
        for number, line in enumerate(self.lines, 1):
            found.extend(line.problems(number))
        codes = self.codes
        if len(codes) > MAX_PRODUCTS:
            found.append(f"в одном документе не больше {MAX_PRODUCTS} кодов — "
                         "разделите продажу")
        broken = [code for code in codes if not codes_module.parse(code).ki]
        if broken:
            found.append(f"кодов, которые не разбираются: {len(broken)} "
                         f"(первый: {broken[0][:40]!r})")
        if len(set(self.identifiers)) != len(codes):
            found.append("в документе есть одинаковые коды")
        if len(self.file_id) > 255:
            found.append("идентификаторы ЭДО слишком длинные для имени файла")
        return found

    @property
    def file_id(self) -> str:
        """Имя файла без расширения: адресат, отправитель, дата, GUID и признаки."""
        day = self.created.strftime("%Y%m%d")
        flags = FILE_FLAGS if self.marked else FILE_FLAGS_UNMARKED
        return (f"{FILE_PREFIX}_{self.buyer.edo_id.strip()}_{self.seller.edo_id.strip()}"
                f"_{day}_{self.guid}_{flags}")

    @property
    def file_name(self) -> str:
        return self.file_id + ".xml"

    def render(self) -> bytes:
        """Файл УПД: ровно эти байты подписываются и уходят оператору."""
        if problems := self.problems:
            raise SaleProblem("Документ не собран: " + "; ".join(problems) + ".")
        return self._text().encode("cp1251", "xmlcharrefreplace")

    def _text(self) -> str:
        date = _ru_date(self.date)
        seller = self.seller
        maker = f"{seller.title}, ИНН {seller.inn.strip()}"
        lines = ['<?xml version="1.0" encoding="windows-1251"?>',
                 f"<Файл ИдФайл={_attr(self.file_id)} ВерсФорм=\"{FORMAT_VERSION}\" "
                 f"ВерсПрог={_attr(f'RetailCore {__version__}')}>",
                 f'\t<Документ КНД="{KND}" Функция="{FUNCTION}" ПоФактХЖ={_attr(FACT)} '
                 f"НаимДокОпр={_attr(DOCUMENT_NAME)} "
                 f'ДатаИнфПр="{self.created:%d.%m.%Y}" ВремИнфПр="{self.created:%H.%M.%S}" '
                 f"НаимЭконСубСост={_attr(maker)}>",
                 f"\t\t<СвСчФакт НомерДок={_attr(self.number)} ДатаДок=\"{date}\">"]
        lines.extend(seller.xml("СвПрод", "\t\t\t"))
        lines.append("\t\t\t<ГрузОт>")
        lines.append("\t\t\t\t<ОнЖе>он же</ОнЖе>")
        lines.append("\t\t\t</ГрузОт>")
        lines.extend(self.buyer.xml("ГрузПолуч", "\t\t\t"))
        lines.append(f"\t\t\t<ДокПодтвОтгрНом РеквНаимДок={_attr(DOCUMENT_NAME)} "
                     f"РеквНомерДок={_attr(self.number)} РеквДатаДок=\"{date}\"/>")
        lines.extend(self.buyer.xml("СвПокуп", "\t\t\t"))
        lines.append('\t\t\t<ДенИзм КодОКВ="643" НаимОКВ="Российский рубль"/>')
        if self.marked or self.signer.by_poa:
            lines.append("\t\t\t<ИнфПолФХЖ1>")
            if self.marked:
                # Признак вывода из оборота — ради него документ и существует. В
                # УПД только на товар без марок выводить нечего.
                lines.append('\t\t\t\t<ТекстИнф Идентиф="СвВыбытияМАРК" '
                             f'Значен="{self.reason}"/>')
            if self.signer.by_poa:
                # По этим полям «ЭДО Лайт» находит доверенность подписанта и
                # прикладывает её к пакету (документация API, загрузка УПД).
                lines.append('\t\t\t\t<ТекстИнф Идентиф="МЧД" '
                             f"Значен={_attr(self.signer.poa_number)}/>")
                lines.append('\t\t\t\t<ТекстИнф Идентиф="Сведения об информационной системе" '
                             f"Значен={_attr(self.signer.poa_system)}/>")
            lines.append("\t\t\t</ИнфПолФХЖ1>")
        lines.append("\t\t</СвСчФакт>")

        lines.append("\t\t<ТаблСчФакт>")
        for number, line in enumerate(self.lines, 1):
            lines.extend(line.xml(number, self.vat, "\t\t\t"))
        totals = self.totals
        lines.append(f'\t\t\t<ВсегоОпл СтТовБезНДСВсего="{totals.net}" '
                     f'СтТовУчНалВсего="{totals.total}" КолНеттоВс="{self.quantity}">')
        lines.extend(_tax("СумНалВсего", totals.vat, self.vat, "\t\t\t\t"))
        lines.append("\t\t\t</ВсегоОпл>")
        lines.append("\t\t</ТаблСчФакт>")

        lines.append("\t\t<СвПродПер>")
        lines.append(f"\t\t\t<СвПер СодОпер={_attr(OPERATION)} ДатаПер=\"{date}\">")
        if self.basis_name.strip():
            lines.append(f"\t\t\t\t<ОснПер РеквНаимДок={_attr(self.basis_name)} "
                         f"РеквНомерДок={_attr(self.basis_number)} "
                         f"РеквДатаДок=\"{_ru_date(self.basis_date)}\"/>")
        else:
            lines.append("\t\t\t\t<БезДокОснПер>1</БезДокОснПер>")
        lines.append("\t\t\t</СвПер>")
        lines.append("\t\t</СвПродПер>")
        lines.extend(self.signer.xml("\t\t"))
        lines.append("\t</Документ>")
        lines.append("</Файл>")
        return "\r\n".join(lines) + "\r\n"


def lines_from_codes(codes: list[str], names: dict[str, str] | None = None,
                     prices: dict[str, Decimal | None] | None = None,
                     origins: dict[str, str] | None = None,
                     customs: dict[str, str] | None = None) -> list[Line]:
    """Раскладывает коды по товарам — строка на GTIN, в порядке первого скана."""
    grouped: dict[str, list[str]] = {}
    for code in codes:
        gtin = codes_module.parse(code).gtin
        grouped.setdefault(gtin, []).append(code)
    names, prices = names or {}, prices or {}
    origins, customs = origins or {}, customs or {}
    return [Line(gtin=gtin, name=names.get(gtin, ""), price=prices.get(gtin),
                 codes=tuple(items), origin=origins.get(gtin, ""),
                 customs=customs.get(gtin, ""))
            for gtin, items in grouped.items()]


def code_problems(infos: list, seller_inn: str) -> list[str]:
    """Что мешает продать эти коды — по ответу «Честного ЗНАКа».

    Продавать можно только свой код в обороте: чужой или уже выбывший ГИС МТ не
    примет, и выяснится это лишь после подписи покупателя, когда документ уже у
    него. Поэтому коды спрашиваются до отправки, и каждая помеха называется.
    """
    found: list[str] = []
    inn = seller_inn.strip()
    for info in infos:
        label = f"{info.gtin} · {info.serial}" if info.gtin else info.code[:31]
        if not info.found:
            found.append(f"{label}: «Честный ЗНАК» такого кода не знает")
        elif not info.state.sellable:
            found.append(f"{label}: код в состоянии «{info.state.title}», "
                         "а продать можно только код в обороте")
        elif inn and info.owner_inn.strip() != inn:
            found.append(f"{label}: код числится за другим участником "
                         f"(ИНН {info.owner_inn or '—'})")
    return found


def file_name(moment: datetime | None = None) -> str:
    """Имя для «Сохранить как» — то же, что требует формат, решается в `Sale`."""
    moment = moment or datetime.now()
    return f"УПД продажи {moment:%Y-%m-%d %H-%M}.xml"


def _attr(value: str) -> str:
    """Значение атрибута в кавычках, с экранированием и без лишних пробелов."""
    return quoteattr(" ".join(str(value).split()))


def _text(value: str) -> str:
    """Код в тексте узла: «<», «>» и «&» из набора GS1 экранируются."""
    return escape(value, {"'": "&apos;", '"': "&quot;"})


def _fio(surname: str, name: str, patronymic: str) -> str:
    middle = f" Отчество={_attr(patronymic)}" if patronymic.strip() else ""
    return f"<ФИО Фамилия={_attr(surname)} Имя={_attr(name)}{middle}/>"


def _tax(tag: str, value: Decimal, vat: str, indent: str) -> list[str]:
    inner = f"<БезНДС>{NO_VAT}</БезНДС>" if vat == NO_VAT else f"<СумНал>{value}</СумНал>"
    return [f"{indent}<{tag}>", f"{indent}\t{inner}", f"{indent}</{tag}>"]


def _ru_date(value: str) -> str:
    """ГГГГ-ММ-ДД → ДД.ММ.ГГГГ, как требует формат."""
    year, month, day = value.strip().split("-")
    return f"{day}.{month}.{year}"
