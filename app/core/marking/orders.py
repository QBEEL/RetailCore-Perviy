"""Заказы кодов в СУЗ: что заказано, что готово, сколько осталось в буфере.

Пока только чтение, и это не полумера. Создание заказа расходует коды, а состав
запроса у него зависит от товарной группы и способа выпуска; выдумать его —
значит однажды заказать не то и не столько. Ответ же на чтение показывает
устройство заказов настоящими именами полей, и следующий шаг делается по факту,
а не по догадке.

Путь и состав ответа проверены обращением к боевой СУЗ, а не взяты из описания.
Выяснились они прогоном `tools/suz_probe.py`: с действующим токеном запрос
доходит до маршрутизации, и коды ответов начинают различать — 405 «такого пути
нет» против 400 «путь есть, данные не те». Без токена все пути отвечали
одинаково, и угадать было нечем. Так и нашлось, что список живёт по
`/order/list`, а `/orders`, `/orders/list` и `/order` — все 405.

Отсюда и терпимость разбора: имена полей ищутся списком, а неузнанный ответ не
теряется — он попадает в журнал целиком, чтобы по нему можно было разобраться.
Единственное, чего разбор не делает, — не выдаёт незнакомое состояние за
знакомое: заказ с состоянием, которого мы не знаем, показывается как есть.

Буфер здесь — не деталь реализации, а то, ради чего заказы и смотрят: коды
выдаются из него порциями, и «сколько ещё осталось» — единственный способ
узнать, хватит ли их на приёмку, не заказывая вслепую ещё раз.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Sequence

from .. import appdata
from . import crypto, transport
from .models import Contour
from .suz import Credentials, TOKEN_HEADER, apply

# Список заказов участника оборота. Чтение: ничего не создаёт и кодов не тратит.
#
# Путь именно такой, проверено обращением. Похожие `/orders`, `/orders/list` и
# `/order` отвечают 405 «Метод не поддерживается» — единственного числа с
# `/list` здесь не угадать, его выяснили прогоном `tools/suz_probe.py`.
ORDERS_PATH = "/order/list"

# Состояние одного заказа по его номеру. Существует: с выдуманным номером
# отвечает «Идентификатор заказа … не найден», код 3360.
ORDER_STATUS_PATH = "/order/status"

# Создание заказа. Единственный расходный метод здесь.
ORDER_PATH = "/order"

# Заголовок, которым уходит открепленная подпись тела заказа. Тем же
# пользуется ПРИНТМАРКИ — `X-Signature` лежит в её библиотеке рядом с
# `clientToken` и адресом заказа.
SIGNATURE_HEADER = "X-Signature"

# Потолок количества на один товар. Число сказала сама СУЗ, отвечая на заказ с
# нулём: «количество должно быть между 1 и 2000000». ПРИНТМАРКИ держит свой,
# более строгий предел в 150 000 — но это её ограничение, а не системы, и
# выдавать его за общее значит запрещать то, что разрешено.
MAX_QUANTITY = 2_000_000

# Способ оплаты по умолчанию — из настоящего заказа участника. Он зависит от
# договора с ЦРПТ, а не от товара, поэтому справочника ему нет.
DEFAULT_PAYMENT = 2

# Журнал, в который попадает ответ незнакомого вида. Разбирать его вручную
# лучше по записи, чем по пересказу пользователя.
LOG_FILE = "marking.log"


# Состояния заказа и буфера приводятся к человеческому виду только там, где мы
# в них уверены. Незнакомое значение показывается как пришло — выдать его за
# «готов» значит однажды пообещать коды, которых нет.
ORDER_STATUS_TITLES: dict[str, str] = {
    "CREATED": "Создан",
    "PENDING": "Ожидает",
    "DECLINED": "Отклонён",
    "APPROVED": "Подтверждён",
    "READY": "Готов",
    "CLOSED": "Закрыт",
    "EXPIRED": "Просрочен",
}

BUFFER_STATUS_TITLES: dict[str, str] = {
    "PENDING": "Готовится",
    "ACTIVE": "Коды готовы",
    "EXHAUSTED": "Исчерпан",
    "REJECTED": "Отклонён",
    "CLOSED": "Закрыт",
}


@dataclass(frozen=True, slots=True)
class Template:
    """Шаблон кода маркировки: какой код выпустит СУЗ.

    Номер шаблона сам по себе не значит для человека ничего, и требовать его
    выбора «по числу» — значит требовать знания, которого у категорийного
    менеджера нет и быть не должно. Поэтому рядом с номером всегда идёт то, чем
    шаблоны на самом деле различаются: длина серийного номера и наличие
    криптохвоста.
    """

    id: int
    serial: int
    crypto: bool
    note: str = ""

    @property
    def title(self) -> str:
        tail = "с криптохвостом" if self.crypto else "без криптохвоста"
        note = f" · {self.note}" if self.note else ""
        return f"{self.id} · серийный номер {self.serial} знаков, {tail}{note}"


# Справочник шаблонов из руководства программиста СУЗ-Облако (RU
# 15861920.620111-04 33 07, раздел «Шаблон КМ»). Не догадка и не наблюдение: у
# каждой товарной группы свой набор, и чужой шаблон СУЗ отвергает прямо — «Для
# данной товарной группы "Духи и туалетная вода" невозможно использовать
# выбранный шаблон КМ 10».
#
# Порядок внутри группы — порядок предпочтения: первым идёт тот, что
# подставится, если своей истории заказов ещё нет.
TEMPLATES: dict[str, tuple[Template, ...]] = {
    "shoes": (Template(1, 13, True),),
    "perfumery": (Template(9, 13, True),),
    "lp": (Template(10, 13, True),),
    "otp": (Template(14, 7, False, "GS1"),
            Template(15, 7, False, "не GS1, с МРЦ")),
    "water": (Template(16, 13, False),),
    "milk": (Template(20, 6, False),),
    "biologically_active_food_supplements": (Template(30, 13, True),
                                             Template(23, 13, False)),
    "antiseptic": (Template(31, 13, True), Template(25, 13, False)),
    "cosmetics": (Template(46, 6, True), Template(47, 6, False)),
}


def templates_for(group: str) -> tuple[Template, ...]:
    """Шаблоны товарной группы. Пусто означает «в справочнике её нет»."""
    return TEMPLATES.get(str(group or "").strip().lower(), ())


def default_template(group: str) -> int:
    """Чем заказывать эту группу, если своей истории ещё нет."""
    known = templates_for(group)
    return known[0].id if known else 0


def template_fits(group: str, template_id: int) -> bool:
    """Годится ли шаблон для группы. Незнакомая группа — не повод запрещать."""
    known = templates_for(group)
    return not known or any(item.id == int(template_id) for item in known)


class ReleaseMethod(str, Enum):
    """Способ выпуска товара в оборот — зачем именно нужны коды.

    Названия дословно те, что показывает ПРИНТМАРКИ: она давно в работе, и
    называть в двух программах одно и то же по-разному — верный способ выбрать
    не то. От способа зависит и состав заказа: производству и импорту нужны
    сведения о площадке, остаткам и перемаркировке — нет.
    """

    REMAINS = "REMAINS"
    PRODUCTION = "PRODUCTION"
    IMPORT = "IMPORT"
    REMARK = "REMARK"
    COMMISSION = "COMMISSION"
    REAPPLY = "REAPPLY"

    @property
    def title(self) -> str:
        return RELEASE_TITLES[self]

    @property
    def needs_production(self) -> bool:
        """Нужны ли сведения о производственной площадке."""
        return self in (ReleaseMethod.PRODUCTION, ReleaseMethod.IMPORT)


RELEASE_TITLES: dict[ReleaseMethod, str] = {
    ReleaseMethod.REMAINS: "Маркировка остатков",
    ReleaseMethod.PRODUCTION: "Производство в РФ",
    ReleaseMethod.IMPORT: "Ввезен в РФ (Импорт)",
    ReleaseMethod.REMARK: "Перемаркировка",
    ReleaseMethod.COMMISSION: "Принят на комиссию от физического лица",
    ReleaseMethod.REAPPLY: "Маркировка вне производства или импорта",
}


class MethodUnknown(transport.MarkingError):
    """Пути нет у самой СУЗ, а не отказ в правах или ошибка в данных.

    Отдельным исключением, потому что лечится оно иначе всех прочих: не сменой
    реквизитов и не повтором, а выяснением настоящего адреса метода.
    """


@dataclass(slots=True)
class Buffer:
    """Коды по одному товару внутри заказа.

    `left` — сколько ещё можно забрать. Именно это число отвечает на вопрос
    «хватит ли», а `total` говорит лишь о том, сколько заказывали когда-то.
    """

    gtin: str = ""
    left: int = 0
    available: int = 0
    total: int = 0
    passed: int = 0
    status: str = ""
    exhausted: bool = False
    expires_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return BUFFER_STATUS_TITLES.get(self.status.upper(), self.status or "—")

    @property
    def ready(self) -> bool:
        return self.left > 0 and not self.exhausted

    @property
    def expired(self) -> bool:
        """Буфер просрочен. Коды из него уже не забрать, сколько бы ни осталось."""
        return bool(self.expires_at and self.expires_at < datetime.now())


@dataclass(slots=True)
class Order:
    """Заказ кодов. Состояние — то, что ответила СУЗ, а не наша догадка."""

    id: str = ""
    status: str = ""
    product_group: str = ""
    payment: str = ""
    created_at: datetime | None = None
    declined: str = ""
    buffers: list[Buffer] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return ORDER_STATUS_TITLES.get(self.status.upper(), self.status or "—")

    @property
    def known(self) -> bool:
        """Знаем ли мы это состояние. Неизвестное показывается дословно."""
        return self.status.upper() in ORDER_STATUS_TITLES

    @property
    def left(self) -> int:
        """Сколько кодов ещё можно забрать по всему заказу."""
        return sum(buffer.left for buffer in self.buffers)

    @property
    def passed(self) -> int:
        """Сколько кодов уже получено. Разница с заказанным — это и есть остаток."""
        return sum(buffer.passed for buffer in self.buffers)

    @property
    def products(self) -> int:
        return len(self.buffers)

    @property
    def expiring(self) -> bool:
        """Есть ли просроченные буферы с неполученными кодами.

        Про такие молчать нельзя: коды в них ещё числятся, а забрать их уже
        нельзя, и «доступно 300» без оговорки было бы обманом.
        """
        return any(buffer.expired and buffer.left > 0 for buffer in self.buffers)


@dataclass(slots=True)
class Defaults:
    """Шаблон кода и способ оплаты для товарной группы.

    Оба числа нельзя ни угадать, ни спросить у СУЗ: метода, который перечислял
    бы шаблоны, у неё нет — проверено, `/templates` и подобные отвечают 405. Зато
    они есть в прошлых заказах самого участника, и это источник лучше любого
    справочника: он не «как бывает вообще», а «как у вас».
    """

    template_id: int = 0
    payment_type: int = DEFAULT_PAYMENT
    # Откуда взято. Пустое означает «ниоткуда» — тогда числа показывать как
    # готовые нельзя, о них нужно спрашивать.
    since: datetime | None = None
    group: str = ""

    @property
    def known(self) -> bool:
        return bool(self.group)


def defaults_for(group: str, known: Sequence[Order]) -> Defaults:
    """Чем заказывали эту группу в прошлый раз.

    Берётся самый свежий заказ группы: шаблоны меняются вместе с товарной
    группой, и позапрошлогодний ответил бы на вопрос вчерашним днём.
    """
    code = str(group or "").strip().lower()
    best: Order | None = None
    # Справочник — основа, история — уточнение: она знает, каким из нескольких
    # шаблонов группы участник пользуется на самом деле.
    fallback = Defaults(template_id=default_template(code))
    for order in known:
        if order.product_group.strip().lower() != code or not order.buffers:
            continue
        if best is None or _newer(order, best):
            best = order
    if best is None:
        return fallback
    buffer = best.buffers[0]
    template = (_number({"t": buffer.raw.get("templateId")}, ("t",))
                or fallback.template_id)
    payment = _number({"p": best.raw.get("paymentType")}, ("p",)) or DEFAULT_PAYMENT
    return Defaults(template_id=template, payment_type=payment,
                    since=best.created_at, group=best.product_group)


def _newer(one: Order, other: Order) -> bool:
    if one.created_at and other.created_at:
        return one.created_at > other.created_at
    return bool(one.created_at)


@dataclass(slots=True)
class Line:
    """Строка заказа: сколько кодов и на какой товар."""

    gtin: str = ""
    quantity: int = 0
    template_id: int = 0
    cis_type: str = "UNIT"
    # Кто придумывает серийные номера. `OPERATOR` — оператор, и это обычный
    # случай; `SELF_MADE` требует передать номера списком, а придумывать их
    # самим незачем.
    serial_type: str = "OPERATOR"

    def as_json(self) -> dict[str, Any]:
        return {
            "gtin": self.gtin.strip(),
            "quantity": int(self.quantity),
            "serialNumberType": self.serial_type,
            "templateId": int(self.template_id),
            "cisType": self.cis_type,
        }


@dataclass(slots=True)
class Request:
    """Заказ целиком — до отправки. Проверяется здесь же, а не сервером."""

    product_group: str = ""
    method: ReleaseMethod = ReleaseMethod.REMAINS
    contact: str = ""
    payment_type: int = DEFAULT_PAYMENT
    lines: list[Line] = field(default_factory=list)
    # Шаблоны, которыми участник уже заказывал эту группу. Справочник —
    # основание, но не последняя инстанция: если СУЗ приняла шаблон однажды,
    # значит он годится, даже когда в руководстве его нет. Реальность старше
    # документа.
    seen_templates: frozenset[int] = field(default_factory=frozenset)

    @property
    def total(self) -> int:
        return sum(int(line.quantity) for line in self.lines)

    @property
    def problems(self) -> tuple[str, ...]:
        """Что помешает отправке. Проверяется до сети — и до денег.

        Пределы не выдуманы: границу «между 1 и 2000000» СУЗ назвала сама,
        отвечая на заказ с нулевым количеством, а запрет на одинаковые коды
        товаров ПРИНТМАРКИ проверяет теми же словами.
        """
        found: list[str] = []
        if not self.product_group.strip():
            found.append("не выбрана товарная группа")
        if not self.contact.strip():
            found.append("не указано контактное лицо")
        if not self.lines:
            found.append("в заказе нет ни одного товара")
        for line in self.lines:
            if not line.gtin.strip():
                found.append("у строки заказа пустой код товара")
                break
        if any(int(line.quantity) <= 0 for line in self.lines):
            found.append("количество должно быть больше нуля")
        codes = [line.gtin.strip() for line in self.lines if line.gtin.strip()]
        if len(codes) != len(set(codes)):
            found.append("одинаковые коды товаров в заказе")
        if any(int(line.quantity) > MAX_QUANTITY for line in self.lines):
            found.append(
                f"на один товар можно заказать не больше {MAX_QUANTITY} кодов")
        # Чужой шаблон СУЗ отвергает — но сказать об этом можно и здесь, до
        # обращения к ключу и до пароля к контейнеру.
        if any(int(line.template_id) <= 0 for line in self.lines):
            found.append("не выбран шаблон кода маркировки")
        wrong = {int(line.template_id) for line in self.lines
                 if int(line.template_id) > 0
                 and int(line.template_id) not in self.seen_templates
                 and not template_fits(self.product_group, line.template_id)}
        if wrong:
            fits = ", ".join(str(item.id)
                             for item in templates_for(self.product_group))
            found.append(
                f"шаблон {', '.join(str(n) for n in sorted(wrong))} не годится "
                f"для этой товарной группы — подойдёт {fits}")
        if self.method.needs_production:
            found.append(
                f"«{self.method.title}» требует сведений о производственной "
                "площадке — они пока не подключены")
        return tuple(found)

    def as_json(self) -> dict[str, Any]:
        """Тело запроса. Состав и порядок — те же, что у ПРИНТМАРКИ."""
        return {
            "productGroup": self.product_group.strip(),
            "contactPerson": self.contact.strip(),
            "releaseMethodType": self.method.value,
            "createMethodType": "SELF_MADE",
            "paymentType": int(self.payment_type),
            "products": [line.as_json() for line in self.lines],
        }


def create(credentials: Credentials, request: Request, thumbprint: str,
           contour: Contour = Contour.SANDBOX) -> str:
    """Заводит заказ кодов и возвращает его номер.

    Единственная в модуле операция, которая тратит деньги, и устроена она
    соответственно.

    Заказ **подписывается**, и это не формальность: без подписи СУЗ отвечает «Не
    указано значение параметра "Открепленная подпись в формате Base64"».
    Подпись открепленная — ровно наоборот, чем при входе, — считается по тому же
    телу, что уходит в запрос, и передаётся заголовком `X-Signature`. Значит,
    заказ требует обращения к закрытому ключу и может спросить пароль к
    контейнеру.

    Повторов нет: `retries=0`. Оборванный ответ на создание заказа не означает
    «не дошло» — он означает «неизвестно», и второй такой запрос был бы вторым
    заказом. Поэтому же неизвестный исход не выдаётся за ошибку: `OrderUnknown`
    говорит прямо, что перед повтором надо посмотреть список заказов.
    """
    if gaps := credentials.stripped().missing:
        raise transport.MarkingError(f"Не заполнено: {', '.join(gaps)}.")
    if problems := request.problems:
        raise transport.MarkingError(
            "Заказ не отправлен: " + "; ".join(problems) + ".")
    if not thumbprint.strip():
        raise transport.MarkingError(
            "Заказ нужно подписать, а сертификат не выбран. Выберите его на "
            "вкладке «Проверка кодов».")

    ready = credentials.stripped()
    apply(ready, contour)
    # Подписывается ровно то, что уйдёт в запрос, — иначе подпись не о том.
    # Поэтому тело собирается один раз и дальше идёт готовой строкой.
    payload = json.dumps(request.as_json(), ensure_ascii=False)
    signature = crypto.sign(payload, thumbprint, detached=True)
    try:
        answer = transport.post(
            "suz", ORDER_PATH, contour=contour,
            params={"omsId": ready.oms_id},
            headers={TOKEN_HEADER: ready.token, SIGNATURE_HEADER: signature},
            text_body=payload, retries=0)
    except transport.Offline as error:
        raise OrderUnknown(
            "Связь оборвалась, и что стало с заказом — неизвестно. Обновите "
            "список заказов и посмотрите, появился ли он: повторная отправка "
            f"создала бы второй заказ.\n\nПодробности: {error}") from None

    order_id = _text(answer if isinstance(answer, dict) else {},
                     ("orderId", "order_id", "id"))
    if not order_id:
        raise transport.MarkingError(
            "СУЗ приняла запрос, но номера заказа не вернула. Обновите список "
            "заказов — заказ мог быть создан.")
    return order_id


class OrderUnknown(transport.MarkingError):
    """Заказ отправлен, а судьба его неизвестна.

    Отдельным исключением, потому что лечится оно единственным способом:
    посмотреть список заказов. Повторять нельзя — это были бы вторые деньги.
    """


def orders(credentials: Credentials,
           contour: Contour = Contour.SANDBOX) -> list[Order]:
    """Заказы участника оборота. Ничего не создаёт и кодов не тратит."""
    ready = credentials.stripped()
    if gaps := ready.missing:
        raise transport.MarkingError(f"Не заполнено: {', '.join(gaps)}.")

    apply(ready, contour)
    try:
        answer = transport.get(
            "suz", ORDERS_PATH, contour=contour,
            params={"omsId": ready.oms_id},
            headers={TOKEN_HEADER: ready.token})
    except transport.MarkingError as error:
        if error.status == 405:
            # С действующим токеном 405 значит именно «такого пути нет»: до
            # маршрутизации запрос дошёл, а метода на нём не оказалось. Гадать
            # дальше нельзя — правильный путь выясняется прогоном `suz_probe`,
            # который спрашивает СУЗ, а не догадку.
            raise MethodUnknown(
                "СУЗ не отдаёт список заказов по этому пути. Какой метод у неё "
                "есть на самом деле, покажет прогон tools\\suz_probe.py — он "
                "только читает и ничего не расходует.", 405) from None
        raise
    rows = _rows(answer)
    if rows is None and answer:
        # Ответ пришёл, а списка в нём не нашлось. Промолчать здесь — значит
        # показать «заказов нет» там, где они есть, и отправить человека искать
        # несуществующую ошибку в личном кабинете.
        _remember(answer)
        raise transport.MarkingError(
            "СУЗ ответила на запрос заказов, но состав ответа незнаком. Ответ "
            f"целиком записан в {appdata.path_to(LOG_FILE)} — по нему разбор "
            "можно поправить.")
    return [_order(row) for row in (rows or []) if isinstance(row, dict)]


def _rows(answer: Any) -> list[Any] | None:
    """Список заказов из ответа, как бы он ни назывался.

    `None` означает «списка в ответе нет», и это не то же самое, что пустой
    список: пустой — законный ответ «заказов не заводили».
    """
    if isinstance(answer, list):
        return answer
    if isinstance(answer, dict):
        for name in ("orderInfos", "orders", "orderInfoList", "results", "data"):
            value = answer.get(name)
            if isinstance(value, list):
                return value
    return None


def _order(row: dict) -> Order:
    order = Order(
        id=_text(row, ("orderId", "order_id", "id")),
        status=_text(row, ("orderStatus", "status", "state")),
        product_group=_text(row, ("productGroup", "product_group", "pg")),
        payment=_text(row, ("paymentType", "payment_type")),
        created_at=_moment(row, ("createdTimestamp", "orderDate", "createDate",
                                 "created")),
        declined=_text(row, ("declineReason", "rejectionReason", "errorMessage")),
        raw=row,
    )
    for item in _buffers(row):
        if isinstance(item, dict):
            order.buffers.append(_buffer(item))
    return order


def _buffers(row: dict) -> list[Any]:
    for name in ("buffers", "bufferInfos", "products", "productInfos"):
        value = row.get(name)
        if isinstance(value, list):
            return value
    return []


def _buffer(row: dict) -> Buffer:
    """Один буфер. Состав полей проверен ответом боевой СУЗ.

    `leftInBuffer` и `availableCodes` — разные числа, и путать их нельзя:
    первое говорит, сколько кодов осталось в буфере вообще, второе — сколько
    из них готово к выдаче прямо сейчас.
    """
    return Buffer(
        gtin=_text(row, ("gtin", "GTIN", "productCode")),
        left=_number(row, ("leftInBuffer", "left", "rest")),
        available=_number(row, ("availableCodes", "available")),
        total=_number(row, ("totalCodes", "total", "quantity", "requestedCodes")),
        passed=_number(row, ("totalPassed", "passed", "issuedCodes")),
        status=_text(row, ("bufferStatus", "status", "state")),
        exhausted=bool(row.get("poolsExhausted") or row.get("exhausted")),
        expires_at=_moment(row, ("expiredDate", "expiredTimestamp", "expireDate")),
        raw=row,
    )


def _text(row: dict, names: Sequence[str]) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def _number(row: dict, names: Sequence[str]) -> int:
    for name in names:
        value = row.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return 0


def _moment(row: dict, names: Sequence[str]) -> datetime | None:
    """Дата заказа. Приходит меткой в миллисекундах или строкой ISO."""
    for name in names:
        value = row.get(name)
        if isinstance(value, bool) or value in (None, ""):
            continue
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value) / 1000)
            except (OverflowError, OSError, ValueError):
                continue
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            continue
    return None


def _remember(answer: Any) -> None:
    """Записывает незнакомый ответ целиком — разбирать его будем по нему."""
    try:
        text = json.dumps(answer, ensure_ascii=False, indent=2)[:20000]
    except (TypeError, ValueError):
        text = repr(answer)[:20000]
    appdata.log_event(LOG_FILE, f"Незнакомый ответ СУЗ на {ORDERS_PATH}:\n{text}")
