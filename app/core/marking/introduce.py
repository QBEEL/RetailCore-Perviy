"""Ввод в оборот: «Маркировка остатков» — файл и отправка из программы.

После печати и нанесения коды нужно ввести в оборот. Документ один —
XML «Ввод в оборот. Маркировка остатков» (`<vvod_ostatky version="3">`, шаблон
взят из ПРИНТМАРКИ 1.3.114), — а довезти его до «Честного ЗНАКа» можно двумя
путями:

- **отправить из программы** — документ подписывается сертификатом и уходит
  единым методом создания документов True API (`POST /lk/documents/create`),
  после чего опрашивается его статус;
- **сохранить файл** и загрузить его в личном кабинете руками.

Состав запроса не выдуман: он снят со строк библиотеки ПРИНТМАРКИ
(`marklib.dll`) — `{"document_format": "XML", "product_document": <base64>,
"type": "LP_INTRODUCE_OST_XML", "signature": <base64>}`, адрес
`/lk/documents/create?pg=<группа>`, статус — `GET /doc/list?number=<номер>&pg=`
(версия API v4, см. `status`).
Подписывается откреплённой подписью **тот же текст XML**, который уходит в
`product_document`: подпись считается по тем байтам, что в запросе, иначе ответ —
«подпись невалидна» без единой подсказки.

В поле `ki` идёт код идентификации: товар и серийный номер, без ключа и
значения проверки (так называется поле в самом формате).

Отправка — действие юридически значимое и необратимое, поэтому:

- повторов у запроса нет: оборванный ответ не означает «не дошло», и второй
  такой запрос — второй документ на те же коды;
- номер документа сохраняется сразу после создания, до опроса статуса;
- успехом считаются только `CHECKED_OK` и `ACCEPTED`; всё незнакомое
  показывается как есть и успехом не объявляется.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Sequence
from xml.sax.saxutils import escape

from .. import appdata
from . import codes as codes_module
from . import crypto, session, transport
from .models import Contour

# Сколько товаров в одном документе. Личный кабинет принимает файл целиком, а
# слишком большой отвергает; граница с запасом ниже предела отчёта о нанесении.
MAX_PRODUCTS = 30_000

_TNVED = re.compile(r"^\d{10}$")
_INN = re.compile(r"^(\d{10}|\d{12})$")


class IntroduceProblem(ValueError):
    """Файл собрать нельзя. Текст пригоден для показа."""


@dataclass(frozen=True, slots=True)
class Document:
    """Документ «Ввод в оборот. Маркировка остатков»."""

    inn: str
    tnved: str
    codes: tuple[str, ...]

    @property
    def problems(self) -> list[str]:
        found: list[str] = []
        if not _INN.match(self.inn.strip()):
            found.append("ИНН участника — 10 цифр для организации, 12 для ИП")
        if not _TNVED.match(self.tnved.strip()):
            found.append("ТН ВЭД — ровно 10 цифр (в файле ввода в оборот система принимает только полный код; 4 знака — это группа, а не код)")
        if not self.codes:
            found.append("нет ни одного кода")
        if len(self.codes) > MAX_PRODUCTS:
            found.append(f"в одном документе не больше {MAX_PRODUCTS} кодов — "
                         "разделите партию")
        return found

    # --- как документ уходит в систему (общий разговор с `Remark`) ------------------

    kind = "ostatky"
    title = "Ввод в оборот. Маркировка остатков"
    wire_format = "XML"

    def wire_type(self, product_group: str) -> str:
        return document_type(product_group)

    def render(self, extension: str = ".xml") -> str:
        return build(self)

    @property
    def file_filters(self) -> str:
        return "XML (*.xml)"


# Причины перемаркировки — те, что показывает ПРИНТМАРКИ в окне «Перемаркировка».
# Расшифровки взяты оттуда же; список в окне прокручивается, поэтому здесь все,
# которые удалось прочитать в её справке.
REMARK_CAUSES: dict[str, str] = {
    "KM_SPOILED": "испорчено либо утеряно СИ с КМ",
    "DESCRIPTION_ERRORS": "выявлены ошибки описания товара",
    "RETAIL_RETURN": "возврат товаров с повреждённым СИ/без СИ при розничной "
                     "реализации (возврат от розничного покупателя)",
    "REMOTE_SALE_RETURN": "возврат товаров с повреждённым СИ/без СИ при "
                          "дистанционном способе продажи",
    "LEGAL_RETURN": "возврат от конечного покупателя (юр. лица/ИП)",
    "INTERNAL_RETURN": "решение о реализации товаров, приобретённых в целях, не "
                       "связанных с их реализацией",
    "EEC_EXPORT_RETURN": "возврат ранее экспортированного в ЕАЭС",
}

# Виды первичного документа, встречающиеся в шаблоне ПРИНТМАРКИ.
PRIMARY_DOCUMENT_TYPES: dict[str, str] = {
    "RECEIPT": "Кассовый чек",
    "OTHER": "Иной документ",
}

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class Remark:
    """Документ «Перемаркировка» (`LK_REMARK`).

    Товар, на который уже был (или должен был быть) другой код, получает новый.
    Новые коды берутся из блока СУЗ, выпущенного способом «Перемаркировка»; по
    справке ПРИНТМАРКИ они должны быть в статусе «Эмитирован. Получен».

    Предыдущий КИ (`previous`) обязателен для причины `DESCRIPTION_ERRORS`, а для
    `KM_SPOILED` и возвратов указывается по желанию: так сказано в той же справке.
    Когда его нет, страну, цвет и размер система берёт из документа — их нельзя
    взять из предыдущего КИ.
    """

    inn: str
    tnved: str
    cause: str
    date: str
    codes: tuple[str, ...]
    previous: tuple[str, ...] = ()
    country: str = ""
    color: str = ""
    size: str = ""
    doc_type: str = ""
    doc_number: str = ""
    doc_date: str = ""
    doc_name: str = ""

    kind = "remark"
    title = "Перемаркировка"
    wire_format = "MANUAL"

    @property
    def problems(self) -> list[str]:
        found: list[str] = []
        if not _INN.match(self.inn.strip()):
            found.append("ИНН участника — 10 цифр для организации, 12 для ИП")
        if not _TNVED.match(self.tnved.strip()):
            found.append("ТН ВЭД — ровно 10 цифр")
        if self.cause not in REMARK_CAUSES:
            found.append("не выбрана причина перемаркировки")
        if not _DATE.match(self.date.strip()):
            found.append("дата перемаркировки — в виде ГГГГ-ММ-ДД")
        if not self.codes:
            found.append("нет ни одного нового кода")
        if len(self.codes) > MAX_PRODUCTS:
            found.append(f"в одном документе не больше {MAX_PRODUCTS} кодов — "
                         "разделите партию")
        if self.previous and len(self.previous) != len(self.codes):
            found.append(f"предыдущих кодов {len(self.previous)}, а новых "
                         f"{len(self.codes)} — их должно быть поровну, в том же порядке")
        if self.cause == "DESCRIPTION_ERRORS" and not self.previous:
            found.append("для причины DESCRIPTION_ERRORS нужен предыдущий КИ "
                         "по каждому новому коду")
        if self.country.strip() and not re.match(r"^\d{3}$", self.country.strip()):
            found.append("страна — трёхзначный код ОКСМ (Россия 643, Китай 156)")
        document = [self.doc_type, self.doc_number, self.doc_date]
        if any(item.strip() for item in document) and not all(
                item.strip() for item in document):
            found.append("первичный документ заполняется целиком: вид, номер и дата")
        if self.doc_type == "OTHER" and not self.doc_name.strip():
            found.append("для иного первичного документа нужно его наименование")
        if self.doc_type and self.doc_type not in PRIMARY_DOCUMENT_TYPES:
            found.append("неизвестный вид первичного документа")
        if self.doc_date.strip() and not _DATE.match(self.doc_date.strip()):
            found.append("дата первичного документа — в виде ГГГГ-ММ-ДД")
        return found

    def wire_type(self, product_group: str) -> str:
        if not product_group.strip():
            raise IntroduceProblem("Не указана товарная группа блока.")
        return "LK_REMARK"

    def _products(self) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for index, code in enumerate(self.codes):
            item: dict[str, str] = {}
            if self.previous:
                item["last_uin"] = identification(self.previous[index])
            item["new_uin"] = identification(code)
            item["tnved_10"] = self.tnved.strip()
            for name, value in (("production_country", self.country),
                                ("color", self.color), ("product_size", self.size),
                                ("primary_document_type", self.doc_type),
                                ("primary_document_custom_name",
                                 self.doc_name if self.doc_type == "OTHER" else ""),
                                ("primary_document_number", self.doc_number),
                                ("primary_document_date", self.doc_date)):
                if value.strip():
                    item[name] = value.strip()
            items.append(item)
        return items

    def render(self, extension: str = ".json") -> str:
        """Текст документа: JSON — тело запроса и файл, XML — файл для кабинета."""
        if problems := self.problems:
            raise IntroduceProblem("Документ не собран: " + "; ".join(problems) + ".")
        if extension.lower() == ".xml":
            return self._xml()
        body = {"participant_inn": self.inn.strip(),
                "remarking_date": self.date.strip(),
                "remarking_cause": self.cause,
                "products": self._products()}
        return json.dumps(body, ensure_ascii=False, indent=2) + "\n"

    def _xml(self) -> str:
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<remark version="7">',
                 f"  <trade_participant_inn>{escape(self.inn.strip())}"
                 "</trade_participant_inn>",
                 f"  <remark_date>{escape(self.date.strip())}</remark_date>",
                 f"  <remark_cause>{escape(self.cause)}</remark_cause>",
                 "  <products_list>"]
        tags = {"last_uin": "last_ki", "new_uin": "new_ki", "tnved_10": "tnved_code_10"}
        for item in self._products():
            lines.append("    <product>")
            for name, value in item.items():
                tag = tags.get(name, name)
                if tag in ("last_ki", "new_ki"):
                    lines.append(f"      <{tag}><![CDATA[{_cdata(value)}]]></{tag}>")
                else:
                    lines.append(f"      <{tag}>{escape(value)}</{tag}>")
            lines.append("    </product>")
        lines.append("  </products_list>")
        lines.append("</remark>")
        return "\n".join(lines) + "\n"

    @property
    def file_filters(self) -> str:
        return "JSON (*.json);;XML (*.xml)"


def identification(code: str) -> str:
    """Код идентификации из полного кода маркировки.

    Неразобранный код не угадывается: документ с придуманным КИ личный кабинет
    примет, а система отвергнет позже и по другой причине.
    """
    parsed = codes_module.parse(code)
    if not parsed.ki:
        raise IntroduceProblem(
            f"Код не разбирается и в документ не попадёт: {code[:40]!r}")
    return parsed.ki


def build(document: Document) -> str:
    """Текст XML. Каждый КИ — в CDATA, как в шаблоне ПРИНТМАРКИ."""
    if problems := document.problems:
        raise IntroduceProblem("Документ не собран: " + "; ".join(problems) + ".")
    tnved = escape(document.tnved.strip())
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<vvod_ostatky version="3">',
        f"  <trade_participant_inn>{escape(document.inn.strip())}"
        "</trade_participant_inn>",
        "  <products_list>",
    ]
    for code in document.codes:
        lines.append("    <product>")
        lines.append(f"      <ki><![CDATA[{_cdata(identification(code))}]]></ki>")
        lines.append(f"      <tnved_code>{tnved}</tnved_code>")
        lines.append("    </product>")
    lines.append("  </products_list>")
    lines.append("</vvod_ostatky>")
    return "\n".join(lines) + "\n"


def _cdata(text: str) -> str:
    # Конец секции CDATA в самих данных разорвал бы разметку.
    return text.replace("]]>", "]]]]><![CDATA[>")


def file_name(gtin: str, moment: datetime | None = None, extension: str = ".xml",
              title: str = "Ввод в оборот") -> str:
    moment = moment or datetime.now()
    return f"{title} {gtin} {moment:%Y-%m-%d %H-%M}{extension}"


def split(codes: Sequence[str] | Iterable[str]) -> list[tuple[str, ...]]:
    """Делит длинный список кодов на документы допустимого размера."""
    items = tuple(codes)
    return [items[start:start + MAX_PRODUCTS]
            for start in range(0, len(items), MAX_PRODUCTS)]


# --- отправка в «Честный ЗНАК» ----------------------------------------------------------

CREATE_PATH = "/lk/documents/create"
LIST_PATH = "/doc/list"
INFO_PATH = "/doc/{doc_id}/info"
DOCUMENT_FORMAT = "XML"

# Тип документа зависит от товарной группы. Остатки заводятся только по группам,
# для которых СУЗ открывает способ «Маркировка остатков»; по остальным отправка
# не подключена — не потому что нельзя, а потому что тип не проверен.
DOCUMENT_TYPES: dict[str, str] = {"lp": "LP_INTRODUCE_OST_XML"}

# Сколько ждать итога и как часто спрашивать. Минута — столько ждёт и ПРИНТМАРКИ.
WAIT_SECONDS = 60
POLL_SECONDS = 3

STATUS_TITLES: dict[str, str] = {
    "IN_PROGRESS": "Обрабатывается",
    "WAIT_FOR_CONTINUATION": "Ожидает дополнительную проверку",
    "WAIT_ACCEPTANCE": "Прошёл проверку, ожидает приёмку",
    "WAIT_PARTICIPANT_REGISTRATION": "Ожидает регистрации участника в ГИС МТ",
    "ACCEPTED": "Принят",
    "CHECKED_OK": "Проверен, коды введены в оборот",
    "CHECKED_NOT_OK": "Отклонён: проверка не пройдена",
    "PROCESSING_ERROR": "Ошибка обработки",
    "PARSE_ERROR": "Отклонён: файл не прошёл проверку формата",
    "PARSING_ERROR": "Отклонён: файл не прошёл проверку формата",
}
SUCCESS = frozenset({"CHECKED_OK", "ACCEPTED"})
# Ожидание — единственное, что не итог. Всё остальное, кроме успеха, итог: либо
# отказ, либо статус, которого мы не знаем.
PENDING = frozenset({"IN_PROGRESS", "WAIT_FOR_CONTINUATION", "WAIT_ACCEPTANCE",
                     "WAIT_PARTICIPANT_REGISTRATION"})
FAILURE = frozenset({"CHECKED_NOT_OK", "PROCESSING_ERROR", "PARSE_ERROR",
                     "PARSING_ERROR"})


class IntroduceUnknown(transport.MarkingError):
    """Документ отправлен, а судьба его неизвестна.

    Лечится единственным способом: посмотреть документы в личном кабинете.
    Повторять нельзя — это был бы второй документ на те же коды.
    """


@dataclass(frozen=True, slots=True)
class DocStatus:
    """Что система сообщила о документе."""

    code: str = ""
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def title(self) -> str:
        if not self.code:
            return "нет данных"
        return STATUS_TITLES.get(self.code.upper(), self.code)

    @property
    def done(self) -> bool:
        return self.code.upper() in SUCCESS

    @property
    def failed(self) -> bool:
        """Отказ. Узнаётся и по имени: `*_ERROR`, `*_NOT_OK`, `REJECTED` — не успех."""
        code = self.code.upper()
        return code in FAILURE or code.endswith("_ERROR") or code.endswith("_NOT_OK") \
            or code in ("REJECTED", "CANCELLED", "ERROR")

    @property
    def pending(self) -> bool:
        """Итога ещё нет. Незнакомый статус сюда не относится: он не итог и не ожидание."""
        return self.code.upper() in PENDING

    @property
    def text(self) -> str:
        tail = ": " + "; ".join(self.errors) if self.errors else ""
        return f"{self.title}{tail}"


@dataclass(frozen=True, slots=True)
class Sent:
    """Итог отправки: номер документа и последний известный статус."""

    doc_id: str
    status: DocStatus


def document_type(product_group: str) -> str:
    kind = DOCUMENT_TYPES.get(product_group.strip().lower())
    if not kind:
        raise IntroduceProblem(
            f"Для товарной группы «{product_group or '—'}» отправка ввода в оборот "
            "из программы не подключена. Сохраните файл и загрузите его в личном "
            "кабинете.")
    return kind


def submit(document: "Document | Remark", thumbprint: str, product_group: str,
           contour: Contour = Contour.SANDBOX) -> str:
    """Подписывает документ и отправляет его. Возвращает номер документа.

    Единственная операция модуля, которую нельзя отменить: повторов нет. Вид
    документа (остатки или перемаркировка) определяет его формат и тип: остатки
    уходят XML-ом (`LP_INTRODUCE_OST_XML`), перемаркировка — JSON-ом `LK_REMARK`,
    как в ПРИНТМАРКИ.
    """
    kind = document.wire_type(product_group)
    xml = document.render(".xml" if document.wire_format == "XML" else ".json")
    if not thumbprint.strip():
        raise IntroduceProblem(
            "Документ нужно подписать, а сертификат не выбран. Выберите его на "
            "вкладке «Проверка кодов».")
    # Подписывается ровно та строка, что уходит в запрос.
    signature = crypto.sign(xml, thumbprint, detached=True)
    body = {
        "document_format": document.wire_format,
        "product_document": base64.b64encode(xml.encode("utf-8")).decode("ascii"),
        "type": kind,
        "signature": signature,
    }
    try:
        answer = session.authorized(lambda token: transport.post(
            "trueapi", CREATE_PATH, contour=contour,
            params={"pg": product_group.strip().lower()}, body=body,
            token=token, retries=0))
    except transport.Offline as error:
        raise IntroduceUnknown(
            "Связь оборвалась, и неизвестно, дошёл ли документ. Посмотрите "
            "документы в личном кабинете «Честного ЗНАКа»: повторная отправка "
            f"создала бы второй документ на те же коды.\n\nПодробности: {error}"
        ) from None
    doc_id = _document_id(answer)
    if not doc_id:
        raise IntroduceUnknown(
            "Система приняла запрос, но номера документа не вернула. Посмотрите "
            "документы в личном кабинете — документ мог быть создан.")
    return doc_id


def _document_id(answer: Any) -> str:
    if isinstance(answer, str):
        return answer.strip().strip('"')
    if isinstance(answer, dict):
        for name in ("docId", "doc_id", "id", "number", "documentId"):
            if answer.get(name):
                return str(answer[name]).strip()
    return ""


def status(doc_id: str, product_group: str,
           contour: Contour = Contour.SANDBOX) -> DocStatus:
    """Что система знает о документе сейчас. Ничего не меняет.

    Спрашивается способами по очереди. Первый — тот, которым пользуется
    ПРИНТМАРКИ: список документов с отбором по номеру, а он живёт только в
    четвёртой версии True API (в третьей ответ «Метод с указанным URL не
    найден» — на нём первая версия этой функции и споткнулась). Второй — карточка
    документа третьей версии. «Не найден» у одного способа — повод попробовать
    следующий, любая другая ошибка — нет.
    """
    group = product_group.strip().lower()
    attempts: list[tuple[str, str, dict[str, Any]]] = [
        ("trueapi4", LIST_PATH, {"number": doc_id, "pg": group}),
        ("trueapi", INFO_PATH.format(doc_id=doc_id), {"pg": group}),
    ]
    last: transport.MarkingError | None = None
    for system, path, params in attempts:
        try:
            answer = session.authorized(lambda token: transport.get(
                system, path, contour=contour, params=params, token=token))
        except transport.MarkingError as error:
            if not _path_missing(error):
                raise
            last = error
            continue
        result = _parse_status(answer, doc_id)
        if result.code and not (result.done or result.pending):
            # Отказ или незнакомый статус: ответ записывается целиком — по нему
            # разбирается, что не так, — и, если причины в нём нет, она
            # запрашивается у карточки документа.
            _remember(doc_id, answer)
            if not result.errors:
                if extra := _details(doc_id, group, contour):
                    result = DocStatus(result.code, extra)
        return result
    raise last or transport.MarkingError("Статус документа получить не удалось.")


def _details(doc_id: str, group: str, contour: Contour) -> tuple[str, ...]:
    """Причина отказа из карточки документа. Не нашлась — пустой ответ, не ошибка."""
    try:
        answer = session.authorized(lambda token: transport.get(
            "trueapi", INFO_PATH.format(doc_id=doc_id), contour=contour,
            params={"pg": group}, token=token))
    except transport.MarkingError:
        return ()
    if not isinstance(answer, dict):
        return ()
    _remember(doc_id, {key: value for key, value in answer.items()
                       if key != "body" and not isinstance(value, (bytes, bytearray))})
    found: list[str] = []
    for key in ("errors", "errorMessages", "error", "errorMessage", "message",
                "description", "downloadDesc"):
        value = answer.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
        elif isinstance(value, (list, dict)):
            found.extend(_errors(value))
    return tuple(dict.fromkeys(found))


def _remember(doc_id: str, answer: Any) -> None:
    """Ответ о документе — в журнал целиком, чтобы причину можно было разобрать."""
    try:
        text = json.dumps(answer, ensure_ascii=False, indent=2)[:20000]
    except (TypeError, ValueError):
        text = repr(answer)[:20000]
    appdata.log_event("marking.log", f"Статус документа {doc_id}:\n{text}")


def _path_missing(error: transport.MarkingError) -> bool:
    """Ответ «такого метода нет», а не отказ по существу."""
    text = str(error).lower()
    return error.status in (404, 405) or "метод с указанным url не найден" in text \
        or "url не найден" in text


def _parse_status(answer: Any, doc_id: str) -> DocStatus:
    """Статус из ответа — и из списка документов, и из карточки одного."""
    rows: list[dict] = []
    if isinstance(answer, dict):
        listed = answer.get("results")
        rows = [row for row in listed if isinstance(row, dict)] \
            if isinstance(listed, list) else [answer]
    elif isinstance(answer, list):
        rows = [row for row in answer if isinstance(row, dict)]
    if not rows:
        return DocStatus()
    chosen = next((row for row in rows
                   if doc_id in {str(row.get(key) or "")
                                 for key in ("number", "docId", "id", "doc_id")}),
                  rows[0])
    return DocStatus(code=str(chosen.get("status") or "").strip(),
                     errors=_errors(chosen.get("errors")))


def _errors(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    found: list[str] = []
    for item in items:
        if isinstance(item, dict):
            text = " ".join(str(value) for value in item.values() if value)
        else:
            text = str(item)
        if text.strip():
            found.append(text.strip())
    return tuple(found)


def send(document: "Document | Remark", thumbprint: str, product_group: str,
         contour: Contour = Contour.SANDBOX, *,
         on_created: Callable[[str], None] | None = None,
         wait: float = WAIT_SECONDS, interval: float = POLL_SECONDS,
         sleep: Callable[[float], None] = time.sleep) -> Sent:
    """Отправляет документ и ждёт итога не дольше `wait` секунд.

    `on_created(номер)` вызывается сразу после создания, до опроса статуса:
    номер надо сохранить раньше, чем что-либо успеет сломаться. Сбой опроса
    документ не отменяет — он создан, и об этом сообщается вместе с причиной.
    """
    doc_id = submit(document, thumbprint, product_group, contour)
    if on_created is not None:
        on_created(doc_id)
    waited = 0.0
    while True:
        try:
            current = status(doc_id, product_group, contour)
        except transport.MarkingError as error:
            return Sent(doc_id, DocStatus(errors=(f"статус не получен: {error}",)))
        # Итог — успех, отказ или статус, которого мы не знаем: ждать у незнакомого
        # нечего, а выдать его за успех нельзя.
        if current.code and not current.pending:
            return Sent(doc_id, current)
        if waited >= wait:
            return Sent(doc_id, current)
        sleep(interval)
        waited += interval
