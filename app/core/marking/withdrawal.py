"""Вывод из оборота: документ «Вывод из оборота» (`LK_RECEIPT`, JSON).

Товар уходит из оборота не сам: розничная продажа, фасовка, уничтожение, утрата
и прочее оформляются документом, в котором перечислены коды маркировки и сказано,
почему они выбывают. После него код нельзя продать повторно, а отменяется вывод
не для каждой причины — поэтому здесь, как и во вводе в оборот, ничего не
отправляется молча.

Состав документа не выдуман: он снят со строк библиотеки ПРИНТМАРКИ
(`marklib.dll`, раздел «Вывод из оборота»). Это JSON: `inn`, `buyer_inn` (по
желанию), `action` — причина (для OTHER ещё `withdrawal_type_other`),
`action_date`, первичный документ (`document_type`, `document_number`,
`document_date`, `primary_document_custom_name`), `kpp` и `fias_id` — место
осуществления деятельности, `state_contract_id` (для госконтракта) и `products` с
кодом `cis` и, когда вывод связан с продажей или передачей, ценой за единицу в
копейках `product_cost` (какие причины требуют цену, видно по тому, где ПРИНТМАРКИ
пишет её, — `COST_CAUSES`).

Место осуществления деятельности (МОД) система ищет по ИНН, КПП и ФИАС: без них
документ отклоняется («МОД по указанным ИНН…, КПП null, ФИАС null не найдены»).
Старый XML версии 8, который описывает та же библиотека, этих полей не знает, и
отправлять его бесполезно, поэтому документ уходит JSON-ом — так же, как
перемаркировка (`LK_REMARK`): `{"document_format": "MANUAL", "product_document":
<base64>, "type": "LK_RECEIPT", "signature": <base64>}`; подписывается тот же
текст JSON.

Номер ККТ, который показывает окно ПРИНТМАРКИ, в этот шаблон не попадает — в
строках библиотеки такого поля нет, и придумывать для него ключ значило бы
отправить в систему то, чего она не ждёт.

Отправка и опрос статуса общие с вводом в оборот (`introduce.send`): документ
здесь устроен так же — `kind`, `title`, `wire_format`, `wire_type`, `render`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import codes as codes_module
from .introduce import MAX_PRODUCTS, IntroduceProblem, _INN

# Причины вывода — в том порядке и теми словами, что в окне ПРИНТМАРКИ. Порядок
# не случаен: первыми идут частые, а «Другое» — последним.
WITHDRAWAL_CAUSES: dict[str, str] = {
    "PACKING": "Фасовка",
    "RETAIL": "Розничная продажа",
    "BY_SAMPLES": "Продажа по образцам",
    "DISTANCE": "Дистанционная продажа",
    "OWN_USE": "Использование для собственных нужд",
    "PRODUCTION_USE": "Использование для производственных целей",
    "VENDING": "Продажа через вендинговый аппарат",
    "BEYOND_EEC_EXPORT": "Экспорт за пределы стран ЕАЭС",
    "CONFISCATION": "Конфискация",
    "DESTRUCTION": "Уничтожение",
    "DONATION": "Безвозмездная передача",
    "EXPIRATION": "Истечение срока годности",
    "LOSS": "Утрата",
    "RECALL": "Отзыв с рынка",
    "RETURN": "Возврат физическому лицу",
    "STATE_CONTRACT": "Продажа по государственному (муниципальному) контракту",
    "STATE_SECRET": "Продажа по сделке с государственной тайной",
    "UTILIZATION": "Утилизация",
    "OTHER": "Другое",
}

# Виды первичного документа — как в списке окна ПРИНТМАРКИ.
PRIMARY_DOCUMENT_TYPES: dict[str, str] = {
    "RECEIPT": "Кассовый чек",
    "SALES_RECEIPT": "Товарный чек",
    "OTHER": "Иной документ",
}

# Причины, при которых у каждого товара должна стоять цена за единицу: так
# ПРИНТМАРКИ решает, писать ли `<cost>`. Для остальных (утрата, уничтожение,
# экспорт…) цены нет, и отправлять её незачем.
COST_CAUSES = frozenset({"PACKING", "RETAIL", "BY_SAMPLES", "DISTANCE", "VENDING",
                         "DONATION", "OWN_USE", "PRODUCTION_USE"})

# Причина по умолчанию и вид документа к ней: чаще всего выводят розничной
# продажей по кассовому чеку.
DEFAULT_CAUSE = "RETAIL"
DEFAULT_DOCUMENT_TYPE = "RECEIPT"

DOCUMENT_TYPE = "LK_RECEIPT"

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KPP = re.compile(r"^\d{9}$")
# Идентификатор адреса в ФИАС (ГАР): GUID. Из кабинета его копируют с пробелами,
# скобками и невидимыми знаками по краям, а иногда без дефисов — всё это
# отбрасывается, ищется сам идентификатор.
_FIAS = re.compile(r"([0-9a-fA-F]{8})-?([0-9a-fA-F]{4})-?([0-9a-fA-F]{4})-?"
                   r"([0-9a-fA-F]{4})-?([0-9a-fA-F]{12})")


def fias(text: str) -> str:
    """ФИАС в каноническом виде («xxxxxxxx-xxxx-…», строчными) или пустая строка."""
    found = _FIAS.search(text or "")
    return "-".join(found.groups()).lower() if found else ""


def kopecks(text: str) -> int | None:
    """Цена из поля «рубли» — в копейки. None, если это не неотрицательное число."""
    try:
        value = Decimal(text.strip().replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0:
        return None
    return int((value * 100).quantize(Decimal(1), ROUND_HALF_UP))


def cause_title(code: str) -> str:
    """«Розничная продажа — RETAIL»: как причина выглядит в списке и в журнале."""
    name = WITHDRAWAL_CAUSES.get(code, "")
    return f"{name} — {code}" if name else code


@dataclass(frozen=True, slots=True)
class Withdrawal:
    """Документ «Вывод из оборота».

    Первичный документ (чек, накладная) заполняется целиком или не заполняется
    вовсе — как и у перемаркировки. Нужен ли он для выбранной причины, решает
    система: заставлять его для всех причин значило бы придумать правило, которого
    у неё нет.
    """

    inn: str
    cause: str
    date: str
    codes: tuple[str, ...]
    cause_other: str = ""
    doc_type: str = ""
    doc_number: str = ""
    doc_date: str = ""
    doc_name: str = ""
    buyer_inn: str = ""
    state_contract_id: str = ""
    # Место осуществления деятельности: ФИАС адреса и КПП (у ИП КПП нет).
    fias_id: str = ""
    kpp: str = ""
    # Цена за единицу в рублях: общая для списка и, по желанию, своя у каждого
    # кода (`code_prices` идёт в том же порядке, что `codes`; пустая — общая).
    price: str = ""
    code_prices: tuple[str, ...] = ()

    # как документ уходит в систему — общий разговор с `introduce.send`
    kind = "withdrawal"
    title = "Вывод из оборота"
    wire_format = "MANUAL"

    @property
    def problems(self) -> list[str]:
        found: list[str] = []
        if not _INN.match(self.inn.strip()):
            found.append("ИНН участника — 10 цифр для организации, 12 для ИП")
        if self.cause not in WITHDRAWAL_CAUSES:
            found.append("не выбрана причина вывода из оборота")
        if self.cause == "OTHER" and not self.cause_other.strip():
            found.append("для причины «Другое» нужно описать её словами")
        if not _DATE.match(self.date.strip()):
            found.append("дата вывода — в виде ГГГГ-ММ-ДД")
        if not self.codes:
            found.append("нет ни одного кода")
        if len(self.codes) > MAX_PRODUCTS:
            found.append(f"в одном документе не больше {MAX_PRODUCTS} кодов — "
                         "разделите список")
        broken = [code for code in self.codes if not codes_module.parse(code).ki]
        if broken:
            found.append(f"кодов, которые не разбираются: {len(broken)} "
                         f"(первый: {broken[0][:40]!r})")
        if len(set(self._keys())) != len(self.codes):
            found.append("в списке есть одинаковые коды")
        document = [self.doc_type, self.doc_number, self.doc_date]
        if any(item.strip() for item in document) and not all(
                item.strip() for item in document):
            found.append("первичный документ заполняется целиком: вид, номер и дата")
        if self.doc_type and self.doc_type not in PRIMARY_DOCUMENT_TYPES:
            found.append("неизвестный вид первичного документа")
        if self.doc_type == "OTHER" and not self.doc_name.strip():
            found.append("для иного первичного документа нужно его наименование")
        if self.doc_date.strip() and not _DATE.match(self.doc_date.strip()):
            found.append("дата первичного документа — в виде ГГГГ-ММ-ДД")
        if self.buyer_inn.strip() and not _INN.match(self.buyer_inn.strip()):
            found.append("ИНН покупателя — 10 цифр для организации, 12 для ИП")
        if not self.fias_id.strip():
            found.append("не указан ФИАС места осуществления деятельности — по нему "
                         "система находит ваш адрес")
        elif not fias(self.fias_id):
            found.append("ФИАС места деятельности не похож на идентификатор вида "
                         "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (получено: "
                         f"{self.fias_id.strip()[:50]!r})")
        if self.kpp.strip() and not _KPP.match(self.kpp.strip()):
            found.append("КПП — 9 цифр")
        elif len(self.inn.strip()) == 10 and not self.kpp.strip():
            found.append("для организации нужен КПП места осуществления деятельности")
        if self.needs_price:
            prices = [self.price_of(index) for index in range(len(self.codes))]
            if empty := sum(1 for item in prices if not item):
                found.append(f"не указана цена за единицу у кодов: {empty} — для "
                             f"причины «{WITHDRAWAL_CAUSES[self.cause]}» она "
                             "обязательна")
            elif any(kopecks(item) is None for item in prices):
                found.append("цена за единицу — число в рублях, например 1490 или 99,90")
        return found

    def price_of(self, index: int) -> str:
        """Цена строки: своя, если задана, иначе общая."""
        own = self.code_prices[index].strip() if index < len(self.code_prices) else ""
        return own or self.price.strip()

    @property
    def needs_price(self) -> bool:
        return self.cause in COST_CAUSES

    def _keys(self) -> list[str]:
        """Что делает код единственным: код идентификации без криптохвоста."""
        return [codes_module.parse(code).ki or code for code in self.codes]

    def wire_type(self, product_group: str) -> str:
        if not product_group.strip():
            raise IntroduceProblem("Не указана товарная группа.")
        return DOCUMENT_TYPE

    def render(self, extension: str = ".json") -> str:
        """Текст JSON: он же уходит в запрос и подписывается."""
        if problems := self.problems:
            raise IntroduceProblem("Документ не собран: " + "; ".join(problems) + ".")
        body: dict[str, object] = {"inn": self.inn.strip()}
        if self.buyer_inn.strip():
            body["buyer_inn"] = self.buyer_inn.strip()
        body["action"] = self.cause
        if self.cause == "OTHER":
            body["withdrawal_type_other"] = self.cause_other.strip()
        body["action_date"] = self.date.strip()
        if self.doc_type:
            body["document_type"] = self.doc_type
            body["document_number"] = self.doc_number.strip()
            body["document_date"] = self.doc_date.strip()
            if self.doc_type == "OTHER":
                body["primary_document_custom_name"] = self.doc_name.strip()
        if self.kpp.strip():
            body["kpp"] = self.kpp.strip()
        body["fias_id"] = fias(self.fias_id)
        if self.cause == "STATE_CONTRACT" and self.state_contract_id.strip():
            body["state_contract_id"] = self.state_contract_id.strip()
        products = []
        for index, code in enumerate(self.codes):
            product: dict[str, object] = {"cis": codes_module.parse(code).ki}
            if self.needs_price:
                product["product_cost"] = kopecks(self.price_of(index))
            products.append(product)
        body["products"] = products
        return json.dumps(body, ensure_ascii=False, indent=2) + "\n"

    @property
    def file_filters(self) -> str:
        return "JSON (*.json)"


def file_name(moment: datetime | None = None, extension: str = ".json") -> str:
    moment = moment or datetime.now()
    return f"Вывод из оборота {moment:%Y-%m-%d %H-%M}{extension}"
