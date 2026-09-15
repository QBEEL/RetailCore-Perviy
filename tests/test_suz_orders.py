"""Заказы кодов в СУЗ: разбор ответа, остаток в буферах, незнакомые состояния.

Образец ответа здесь — настоящий: состав полей снят с боевой СУЗ, а не взят из
описания. Числа и номера заменены, устройство сохранено полностью.

Терпимость разбора к другим именам полей всё равно проверяется: состав ответа
может измениться, а «читалось вчера» — не довод.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.core.marking import orders, suz, transport
from app.core.marking.models import Contour

OMS = "b1a2c3d4-0000-1111-2222-333344445555"
FULL = suz.Credentials(oms_id=OMS, token="токен-устройства-длинный")

# Ответ боевой СУЗ на `/order/list`, поле в поле. Метки времени — в
# миллисекундах; `leftInBuffer` и `availableCodes` — разные числа.
ANSWER = {
    "omsId": OMS,
    "orderInfos": [
        {
            "orderId": "3a8f0000-1111-2222-3333-444455556666",
            "orderStatus": "READY",
            "createdTimestamp": 1754_300_000_000,
            "productGroup": "perfumery",
            "paymentType": "OWN_ACCOUNT",
            "buffers": [
                {"gtin": "04601234567893", "leftInBuffer": 250,
                 "availableCodes": 250, "unavailableCodes": 0,
                 "totalCodes": 1000, "totalPassed": 750,
                 "bufferStatus": "ACTIVE", "poolsExhausted": False,
                 "expiredDate": 1793_670_229_733, "cisType": "UNIT",
                 "templateId": 10},
                {"gtin": "04601234567894", "leftInBuffer": 0,
                 "availableCodes": 0, "unavailableCodes": 0,
                 "totalCodes": 500, "totalPassed": 500,
                 "bufferStatus": "EXHAUSTED", "poolsExhausted": True,
                 "expiredDate": 1793_670_229_733, "cisType": "UNIT",
                 "templateId": 10},
            ],
        }
    ]
}


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    """Профиль на время теста — свой: журнал не должен писаться в настоящий."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def signing(monkeypatch):
    """Подпись вместо КриптоПро: заказ подписывается, но ключа в тестах нет."""
    monkeypatch.setattr(orders.crypto, "sign",
                        lambda data, thumbprint, detached=False: f"подпись({detached})")


@pytest.fixture
def answered(monkeypatch):
    """Ответ СУЗ вместо обращения к сети, вместе с журналом запросов."""
    calls: list = []
    state: dict = {"body": ANSWER}

    def send(prepared, tolerate=()):
        calls.append(prepared)
        return state["body"]

    monkeypatch.setattr(transport, "_send", send)
    transport.limiter.reset()
    return calls, state


def test_заказ_и_остаток_в_буфере_читаются(answered):
    found = orders.orders(FULL, Contour.SANDBOX)

    assert len(found) == 1
    order = found[0]
    assert order.id == "3a8f0000-1111-2222-3333-444455556666"
    assert order.title == "Готов"
    assert order.products == 2
    # Ради этого числа заказы и смотрят: сколько кодов ещё можно забрать.
    assert order.left == 250
    assert order.created_at is not None


def test_исчерпанный_буфер_не_считается_готовым(answered):
    order = orders.orders(FULL, Contour.SANDBOX)[0]
    active, exhausted = order.buffers

    assert active.ready
    assert active.title == "Коды готовы"
    assert not exhausted.ready
    assert exhausted.title == "Исчерпан"


def test_запрос_идёт_по_проверенному_пути(answered):
    """Список живёт по `/order/list`: `/orders` и `/orders/list` отвечают 405."""
    calls, _ = answered

    orders.orders(FULL, Contour.SANDBOX)

    prepared = calls[0]
    assert prepared.full_url.endswith(f"/api/v3/order/list?omsId={OMS}")
    assert prepared.get_header("Clienttoken") == FULL.token
    assert prepared.data is None       # чтение: ничего не создаёт


def test_полученное_и_оставшееся_считаются_отдельно(answered):
    """«Получено» и «доступно» — разные числа, и путать их нельзя."""
    order = orders.orders(FULL, Contour.SANDBOX)[0]

    assert order.passed == 1250
    assert order.left == 250
    assert order.buffers[0].available == 250


def test_просроченный_буфер_с_остатком_помечается(answered):
    """Коды в нём числятся, а забрать их уже нельзя — молчать об этом обман."""
    _, state = answered
    state["body"] = {"orderInfos": [{
        "orderId": "з", "orderStatus": "READY",
        "buffers": [{"gtin": "046", "leftInBuffer": 300, "totalCodes": 300,
                     "bufferStatus": "ACTIVE", "expiredDate": 1_000_000_000_000}],
    }]}

    order = orders.orders(FULL, Contour.SANDBOX)[0]

    assert order.buffers[0].expired
    assert order.expiring
    assert order.left == 300


def test_другие_имена_полей_читаются_так_же(answered):
    """Разбор терпим намеренно: состав ответа обращением не проверен."""
    _, state = answered
    state["body"] = {"orders": [{"id": "заказ-1", "status": "CREATED",
                                 "products": [{"GTIN": "046", "left": 7,
                                               "quantity": 10}]}]}

    order = orders.orders(FULL, Contour.SANDBOX)[0]

    assert order.id == "заказ-1"
    assert order.title == "Создан"
    assert order.left == 7
    assert order.buffers[0].total == 10


def test_голый_список_тоже_считается_ответом(answered):
    _, state = answered
    state["body"] = [{"orderId": "заказ-2", "orderStatus": "PENDING"}]

    assert orders.orders(FULL, Contour.SANDBOX)[0].id == "заказ-2"


def test_незнакомое_состояние_показывается_дословно(answered):
    """Выдать неизвестное состояние за «готов» значит пообещать кодов, которых нет."""
    _, state = answered
    state["body"] = {"orderInfos": [{"orderId": "з", "orderStatus": "WAITING_FOR_SIGN"}]}

    order = orders.orders(FULL, Contour.SANDBOX)[0]

    assert not order.known
    assert order.title == "WAITING_FOR_SIGN"


def test_незнакомый_ответ_не_выдаётся_за_пустой(answered, tmp_path):
    """Пустой список там, где заказы есть, отправил бы искать ошибку не туда."""
    _, state = answered
    state["body"] = {"чего-то": {"новое": 1}}

    with pytest.raises(transport.MarkingError, match="состав ответа незнаком"):
        orders.orders(FULL, Contour.SANDBOX)

    # Ответ записан целиком: разбирать его будем по записи, а не по пересказу.
    written = (tmp_path / "RetailCore" / orders.LOG_FILE).read_text(encoding="utf-8")
    assert "новое" in written


def test_пустой_ответ_это_просто_нет_заказов(answered):
    _, state = answered
    state["body"] = {"orderInfos": []}

    assert orders.orders(FULL, Contour.SANDBOX) == []


def test_несуществующий_метод_отличён_от_ошибки_в_данных(answered, monkeypatch):
    """С действующим токеном 405 значит «такого пути нет», а не «данные не те».

    Лечится это иначе всех прочих отказов: не сменой реквизитов и не повтором,
    а выяснением настоящего адреса метода.
    """
    def refuse(prepared, tolerate=()):
        raise transport.MarkingError("Метод не поддерживается", 405)

    monkeypatch.setattr(transport, "_send", refuse)

    with pytest.raises(orders.MethodUnknown) as failure:
        orders.orders(FULL, Contour.SANDBOX)

    assert "suz_probe" in str(failure.value)
    # Остаётся разновидностью ошибки маркировки: вкладка ловит её наравне с
    # прочими и показывает текст, а не падает на незнакомом типе.
    assert isinstance(failure.value, transport.MarkingError)


def test_прочие_отказы_не_выдаются_за_отсутствие_метода(answered, monkeypatch):
    def refuse(prepared, tolerate=()):
        raise transport.MarkingError("Проверка учетных данных УОТ не пройдена", 400)

    monkeypatch.setattr(transport, "_send", refuse)

    with pytest.raises(transport.MarkingError) as failure:
        orders.orders(FULL, Contour.SANDBOX)

    assert not isinstance(failure.value, orders.MethodUnknown)


def test_без_реквизитов_до_сети_дело_не_доходит(answered):
    calls, _ = answered

    with pytest.raises(transport.MarkingError, match="Не заполнено"):
        orders.orders(suz.Credentials(oms_id=OMS), Contour.SANDBOX)

    assert calls == []


# --- создание заказа ------------------------------------------------------------------

def _request(**kwargs) -> orders.Request:
    request = orders.Request(
        product_group="lp",
        method=orders.ReleaseMethod.REMAINS,
        contact="Иванов Евгений",
        lines=[orders.Line(gtin="04601234567893", quantity=100,
                           template_id=orders.default_template("lp"))],
    )
    for name, value in kwargs.items():
        setattr(request, name, value)
    return request


def test_тело_заказа_собрано_как_у_принтмарки(answered):
    """Состав снят с работающей программы, а не придуман."""
    calls, state = answered
    state["body"] = {"orderId": "новый-заказ", "expectedCompleteTimestamp": 0}

    order_id = orders.create(FULL, _request(), "ААББ", Contour.SANDBOX)

    assert order_id == "новый-заказ"
    prepared = calls[0]
    assert prepared.full_url.endswith(f"/api/v3/order?omsId={OMS}")
    assert prepared.get_method() == "POST"
    body = json.loads(prepared.data)
    assert body == {
        "productGroup": "lp",
        "contactPerson": "Иванов Евгений",
        "releaseMethodType": "REMAINS",
        "createMethodType": "SELF_MADE",
        "paymentType": 2,
        "products": [{"gtin": "04601234567893", "quantity": 100,
                      "serialNumberType": "OPERATOR", "templateId": 10,
                      "cisType": "UNIT"}],
    }


def test_оборванная_связь_не_повторяет_заказ(monkeypatch):
    """Повтор создания — это второй заказ и вторые деньги.

    Транспорт повторяет недоступность сам, и для заказа это выключено: повторов
    ровно ноль, а исход объявлен неизвестным.
    """
    tries = []

    def drop(prepared, tolerate=()):
        tries.append(prepared)
        raise transport.Offline("связь оборвалась")

    monkeypatch.setattr(transport, "_send", drop)
    transport.limiter.reset()

    with pytest.raises(orders.OrderUnknown) as failure:
        orders.create(FULL, _request(), "ААББ", Contour.SANDBOX)

    assert len(tries) == 1
    assert "повторная отправка" in str(failure.value).lower()
    assert "обновите список" in str(failure.value).lower()


def test_ответ_без_номера_заказа_не_считается_удачей(answered):
    _, state = answered
    state["body"] = {"success": True}

    with pytest.raises(transport.MarkingError, match="номера заказа не вернула"):
        orders.create(FULL, _request(), "ААББ", Contour.SANDBOX)


def test_одинаковые_товары_в_заказе_отсекаются_до_сети(answered):
    """ПРИНТМАРКИ проверяет это теми же словами — значит, так проверяет и СУЗ."""
    calls, _ = answered
    twice = _request(lines=[orders.Line(gtin="046", quantity=10, template_id=10),
                            orders.Line(gtin="046", quantity=20, template_id=10)])

    with pytest.raises(transport.MarkingError, match="Одинаковые коды|одинаковые коды"):
        orders.create(FULL, twice, "ААББ", Contour.SANDBOX)

    assert calls == []


def test_потолок_количества_взят_у_самой_суз(answered):
    """«Количество должно быть между 1 и 2000000» — её слова, не наша осторожность.

    ПРИНТМАРКИ держит свой предел в 150 000, но это её ограничение: выдавать
    его за общее значило бы запрещать разрешённое.
    """
    calls, _ = answered
    assert orders.MAX_QUANTITY == 2_000_000

    huge = _request(lines=[orders.Line(gtin="046", quantity=orders.MAX_QUANTITY + 1, template_id=10)])
    assert any("не больше" in problem for problem in huge.problems)

    with pytest.raises(transport.MarkingError):
        orders.create(FULL, huge, "ААББ", Contour.SANDBOX)
    assert calls == []

    # А ровно потолок — разрешён.
    assert _request(lines=[orders.Line(gtin="046", quantity=orders.MAX_QUANTITY,
                                       template_id=10)]).problems == ()


def test_пустой_заказ_и_ноль_штук_не_уходят(answered):
    assert "в заказе нет ни одного товара" in _request(lines=[]).problems
    zero = _request(lines=[orders.Line(gtin="046", quantity=0, template_id=10)])
    assert any("больше нуля" in problem for problem in zero.problems)


def test_без_контактного_лица_и_группы_заказ_не_собирается():
    assert "не указано контактное лицо" in _request(contact=" ").problems
    assert "не выбрана товарная группа" in _request(product_group="").problems


def test_производство_и_импорт_названы_неготовыми_а_не_отправлены():
    """Им нужны сведения о площадке; отправить без них — получить отказ ЦРПТ."""
    for method in (orders.ReleaseMethod.PRODUCTION, orders.ReleaseMethod.IMPORT):
        problems = " ".join(_request(method=method).problems)
        assert "производственной площадке" in problems


def test_остатки_и_перемаркировка_готовы():
    for method in (orders.ReleaseMethod.REMAINS, orders.ReleaseMethod.REMARK,
                   orders.ReleaseMethod.COMMISSION, orders.ReleaseMethod.REAPPLY):
        assert _request(method=method).problems == ()


def test_способы_выпуска_названы_как_в_принтмарки():
    """Одно и то же в двух программах должно называться одинаково."""
    assert orders.ReleaseMethod.REMAINS.title == "Маркировка остатков"
    assert orders.ReleaseMethod.REMARK.title == "Перемаркировка"
    assert orders.ReleaseMethod.IMPORT.title == "Ввезен в РФ (Импорт)"


# --- шаблон и оплата по истории -------------------------------------------------------

def _past(group: str, template: int, payment: int, day: int) -> orders.Order:
    order = orders.Order(id=f"з-{day}", status="READY", product_group=group,
                         created_at=datetime(2026, 8, day),
                         raw={"paymentType": payment})
    order.buffers = [orders.Buffer(gtin="046", raw={"templateId": template})]
    return order


def test_шаблон_берётся_из_прошлого_заказа_этой_группы():
    """Списка шаблонов СУЗ не отдаёт — зато прошлый заказ отвечает точно."""
    known = orders.defaults_for("lp", [_past("perfumery", 12, 1, 1),
                                       _past("lp", 10, 2, 3)])

    assert known.known
    assert (known.template_id, known.payment_type) == (10, 2)
    assert known.since == datetime(2026, 8, 3)


def test_берётся_самый_свежий_заказ_группы():
    """Шаблоны меняются вместе с группой; позапрошлый ответил бы вчерашним днём."""
    known = orders.defaults_for("lp", [_past("lp", 10, 2, 1), _past("lp", 14, 2, 9)])

    assert known.template_id == 14


def test_группа_без_заказов_берёт_шаблон_из_справочника():
    """Чужой шаблон дал бы коды не того вида, а справочник знает нужный."""
    known = orders.defaults_for("milk", [_past("lp", 10, 2, 3)])

    assert not known.known          # своей истории по группе нет
    assert known.since is None
    # Но шаблон всё равно верный: он из руководства СУЗ, а не от соседней группы.
    assert known.template_id == 20
    assert orders.default_template("perfumery") == 9


def test_заказ_без_буферов_в_расчёт_не_идёт():
    empty = orders.Order(id="з", product_group="lp", created_at=datetime(2026, 8, 9))

    assert not orders.defaults_for("lp", [empty]).known


# --- подпись заказа -------------------------------------------------------------------

def test_заказ_подписывается_и_подпись_уходит_заголовком(answered, monkeypatch):
    """Без подписи СУЗ отвечает «Не указано значение параметра "Открепленная
    подпись в формате Base64"». Подпись открепленная — наоборот, чем при входе."""
    calls, state = answered
    state["body"] = {"orderId": "з"}
    signed: list = []

    def sign(data, thumbprint, detached=False):
        signed.append((data, thumbprint, detached))
        return "подпись-в-base64"

    monkeypatch.setattr(orders.crypto, "sign", sign)

    orders.create(FULL, _request(), "ААББ", Contour.SANDBOX)

    prepared = calls[0]
    assert prepared.get_header("X-signature") == "подпись-в-base64"
    assert signed[0][1] == "ААББ"
    # Именно открепленная: присоединённая — это вход, и там наоборот.
    assert signed[0][2] is True


def test_подписывается_ровно_то_что_уходит_в_запрос(answered, monkeypatch):
    """Иначе однажды соберём тело второй раз иначе — и получим «подпись невалидна»."""
    calls, state = answered
    state["body"] = {"orderId": "з"}
    signed: list = []
    monkeypatch.setattr(orders.crypto, "sign",
                        lambda data, thumbprint, detached=False: signed.append(data) or "п")

    orders.create(FULL, _request(), "ААББ", Contour.SANDBOX)

    assert signed[0].encode("utf-8") == calls[0].data


def test_без_сертификата_заказ_не_собирается(answered):
    """Подпись обязательна, и сказать об этом нужно до сети, а не после отказа."""
    calls, _ = answered

    with pytest.raises(transport.MarkingError, match="сертификат не выбран"):
        orders.create(FULL, _request(), "", Contour.SANDBOX)

    assert calls == []


def test_разбор_по_полям_доходит_до_человека():
    """СУЗ кладёт его в `fieldErrors`, а не в `globalErrors` — читать надо оба."""
    body = ('{"fieldErrors":[{"errorCode":3150,"fieldError":"Нельзя использовать '
            'шаблон 999","fieldName":"products[0].templateId"}],"success":false}')

    error = transport._failure(400, body.encode("utf-8"))

    assert "шаблон 999" in str(error)
    assert "products[0].templateId" in str(error)
    assert "3150" in str(error)
    # Общая фраза «отвергла запрос» появляется, только когда сказать нечего.
    assert "отвергла запрос" not in str(error)


# --- справочник шаблонов --------------------------------------------------------------

def test_шаблон_у_каждой_группы_свой():
    """«Для данной товарной группы "Духи и туалетная вода" невозможно
    использовать выбранный шаблон КМ 10» — ответ настоящей СУЗ."""
    assert orders.default_template("perfumery") == 9
    assert orders.default_template("lp") == 10
    assert orders.default_template("shoes") == 1
    assert orders.default_template("milk") == 20

    assert not orders.template_fits("perfumery", 10)
    assert orders.template_fits("perfumery", 9)


def test_чужой_шаблон_отсекается_до_пароля_к_контейнеру(answered):
    """Сказать об этом можно и до обращения к ключу, и до отказа СУЗ."""
    calls, _ = answered
    wrong = _request(product_group="perfumery")   # шаблон в строке — от «lp»

    problems = " ".join(wrong.problems)
    assert "не годится для этой товарной группы" in problems
    assert "подойдёт 9" in problems

    with pytest.raises(transport.MarkingError):
        orders.create(FULL, wrong, "ААББ", Contour.SANDBOX)
    assert calls == []


def test_шаблон_назван_тем_чем_шаблоны_различаются():
    """Номер сам по себе не значит ничего: важны длина серийного и криптохвост."""
    perfumery, = orders.templates_for("perfumery")

    assert perfumery.title.startswith("9 · серийный номер 13 знаков")
    assert "с криптохвостом" in perfumery.title

    # У косметики их два, и различаются они именно этим.
    crypto_tail, short = orders.templates_for("cosmetics")
    assert crypto_tail.id == 46 and "с криптохвостом" in crypto_tail.title
    assert short.id == 47 and "без криптохвоста" in short.title


def test_группа_вне_справочника_не_запрещает_заказ():
    """Справочник — помощь, а не запрет: неизвестной группе шаблон впишут руками."""
    assert orders.templates_for("новая-группа") == ()
    assert orders.template_fits("новая-группа", 123)
    assert orders.default_template("новая-группа") == 0


def test_невыбранный_шаблон_назван_невыбранным():
    """«Шаблон 0 не годится» — загадка; «не выбран шаблон» — указание."""
    empty = _request(lines=[orders.Line(gtin="046", quantity=1)])

    assert "не выбран шаблон кода маркировки" in empty.problems
