"""Вывод из оборота: документ «Вывод из оборота» (`LK_RECEIPT_XML`).

Товар уходит из оборота не сам: розничная продажа, фасовка, уничтожение, утрата
и прочее оформляются документом, в котором перечислены коды маркировки и сказано,
почему они выбывают. После него код нельзя продать повторно, а отменяется вывод
не для каждой причины — поэтому здесь, как и во вводе в оборот, ничего не
отправляется молча.

Состав документа не выдуман: он снят со строк библиотеки ПРИНТМАРКИ
(`marklib.dll`, раздел «Вывод из оборота»). Корень — `<withdrawal version="8">`;
внутри по порядку: `trade_participant_inn`, `buyer_inn` (по желанию),
`withdrawal_type`, `withdrawal_type_other` (для причины OTHER), `withdrawal_date`,
первичный документ (`primary_document_type`, `primary_document_number`,
`primary_document_date`, `primary_document_custom_name`), `state_contract_id`
(для госконтракта) и `products_list` с кодами в `<cis>` внутри CDATA. Уходит он
тем же единым методом создания документов, что и ввод в оборот:
`{"document_format": "XML", "product_document": <base64>, "type":
"LK_RECEIPT_XML", "signature": <base64>}`; подписывается тот же текст XML.

Номер ККТ, который показывает окно ПРИНТМАРКИ, в этот шаблон не попадает — в
строках библиотеки такого поля нет, и придумывать для него тег значило бы
отправить в систему то, чего она не ждёт.

Отправка и опрос статуса общие с вводом в оборот (`introduce.send`): документ
здесь устроен так же — `kind`, `title`, `wire_format`, `wire_type`, `render`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from xml.sax.saxutils import escape

from . import codes as codes_module
from .introduce import MAX_PRODUCTS, IntroduceProblem, _cdata, _INN

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

# Причина по умолчанию и вид документа к ней: чаще всего выводят розничной
# продажей по кассовому чеку.
DEFAULT_CAUSE = "RETAIL"
DEFAULT_DOCUMENT_TYPE = "RECEIPT"

DOCUMENT_TYPE = "LK_RECEIPT_XML"

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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

    # как документ уходит в систему — общий разговор с `introduce.send`
    kind = "withdrawal"
    title = "Вывод из оборота"
    wire_format = "XML"

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
        return found

    def _keys(self) -> list[str]:
        """Что делает код единственным: код идентификации без криптохвоста."""
        return [codes_module.parse(code).ki or code for code in self.codes]

    def wire_type(self, product_group: str) -> str:
        if not product_group.strip():
            raise IntroduceProblem("Не указана товарная группа.")
        return DOCUMENT_TYPE

    def render(self, extension: str = ".xml") -> str:
        """Текст XML: он же уходит в запрос и подписывается."""
        if problems := self.problems:
            raise IntroduceProblem("Документ не собран: " + "; ".join(problems) + ".")
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<withdrawal version="8">',
                 f"  <trade_participant_inn>{escape(self.inn.strip())}"
                 "</trade_participant_inn>"]
        if self.buyer_inn.strip():
            lines.append(f"  <buyer_inn>{escape(self.buyer_inn.strip())}</buyer_inn>")
        lines.append(f"  <withdrawal_type>{escape(self.cause)}</withdrawal_type>")
        if self.cause == "OTHER":
            lines.append("  <withdrawal_type_other>"
                         f"{escape(self.cause_other.strip())}</withdrawal_type_other>")
        lines.append(f"  <withdrawal_date>{escape(self.date.strip())}</withdrawal_date>")
        if self.doc_type:
            lines.append(f"  <primary_document_type>{escape(self.doc_type)}"
                         "</primary_document_type>")
            lines.append("  <primary_document_number>"
                         f"{escape(self.doc_number.strip())}</primary_document_number>")
            lines.append("  <primary_document_date>"
                         f"{escape(self.doc_date.strip())}</primary_document_date>")
            if self.doc_type == "OTHER":
                lines.append("  <primary_document_custom_name>"
                             f"{escape(self.doc_name.strip())}"
                             "</primary_document_custom_name>")
        if self.cause == "STATE_CONTRACT" and self.state_contract_id.strip():
            lines.append("  <state_contract_id>"
                         f"{escape(self.state_contract_id.strip())}</state_contract_id>")
        lines.append("  <products_list>")
        for code in self.codes:
            lines.append("    <product>")
            lines.append("      <cis><![CDATA["
                         f"{_cdata(codes_module.parse(code).ki)}]]></cis>")
            lines.append("    </product>")
        lines.append("  </products_list>")
        lines.append("</withdrawal>")
        return "\n".join(lines) + "\n"

    @property
    def file_filters(self) -> str:
        return "XML (*.xml)"


def file_name(moment: datetime | None = None, extension: str = ".xml") -> str:
    moment = moment or datetime.now()
    return f"Вывод из оборота {moment:%Y-%m-%d %H-%M}{extension}"
