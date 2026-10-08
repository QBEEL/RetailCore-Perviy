"""Продажа с выводом из оборота: УПД 5.03 с признаком `СвВыбытияМАРК` и «ЭДО Лайт».

Состав документа сверен со схемой ФНС `ON_NSCHFDOPPR_1_997_01_05_03_05.xsd` и с
настоящим УПД из Диадока; тесты держат то, что схема и ГИС МТ требуют, —
порядок узлов, признак вывода, код без криптохвоста, имя файла и кодировку. Сеть,
КриптоПро и окна подменяются; главное в поведении — продажа не уходит молча,
непроверенной или с чужими кодами, а оборванная отправка повторяется тем же файлом.
"""
from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.marking import edo, sale, transport
from app.core.marking.codes import GS
from app.core.marking.models import (
    CodeInfo,
    CodeState,
    Contour,
    OperationKind,
    OperationStatus,
)

GTIN = "04680962315457"
OTHER = "03614273069687"
FIRST = f"01{GTIN}215hsFUtaRDI3mf{GS}91EE11{GS}92dGVzdGRhdGE="
SECOND = f"01{GTIN}215abcDEFxyZ01{GS}91EE11{GS}92dGVzdGRhdGE="
THIRD = f"01{OTHER}215QwErTyUiOp12"
FIRST_KI = f"01{GTIN}215hsFUtaRDI3mf"
SECOND_KI = f"01{GTIN}215abcDEFxyZ01"
THIRD_KI = f"01{OTHER}215QwErTyUiOp12"

SELLER = sale.Party(inn="254009895745", name="ИП Саух Мария Николаевна",
                    address="690090, г Владивосток, ул Авроровская, д 17",
                    edo_id="2LT-254009895745")
BUYER = sale.Party(inn="7730306682", kpp="773001001", name='ООО "Бьюти Глобал"',
                   address="121293, г Москва, ул Неверовского, д 10/3",
                   edo_id="2BM-7730306682-773001001-2023")
SIGNER = sale.Signer("Саух", "Мария", "Николаевна", "Индивидуальный предприниматель")


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    transport.limiter.reset()
    return tmp_path


def _lines(prices=None, names=None, **extra):
    return tuple(sale.lines_from_codes(
        [FIRST, SECOND, THIRD],
        names=names if names is not None else {GTIN: "Парфюмерная вода 50 мл",
                                              OTHER: "Туалетная вода 100 мл"},
        prices=prices if prices is not None else {GTIN: Decimal("4990.00"),
                                                  OTHER: Decimal("3333.33")},
        **extra))


def _document(**changes) -> sale.Sale:
    values = dict(number="ПР-15", date="2026-10-08", seller=SELLER, buyer=BUYER,
                  signer=SIGNER, lines=_lines(), vat="22%",
                  guid="11111111-2222-3333-4444-555555555555",
                  created=datetime(2026, 10, 8, 14, 5, 9))
    values.update(changes)
    return sale.Sale(**values)


def _root(document: sale.Sale) -> ET.Element:
    return ET.fromstring(document.render())


# --- состав УПД ---------------------------------------------------------------------

def test_в_упд_стоит_признак_вывода_из_оборота_для_собственных_нужд():
    root = _root(_document())

    [info] = root.findall("Документ/СвСчФакт/ИнфПолФХЖ1/ТекстИнф")
    assert info.attrib == {"Идентиф": "СвВыбытияМАРК", "Значен": "1"}
    production = _root(_document(reason="3"))
    assert production.find("Документ/СвСчФакт/ИнфПолФХЖ1/ТекстИнф").get("Значен") == "3"


def test_упд_версии_503_со_счётом_фактурой_и_передачей():
    root = _root(_document())

    assert root.get("ВерсФорм") == "5.03" and root.get("ВерсПрог").startswith("RetailCore")
    document = root.find("Документ")
    assert document.get("КНД") == "1115131" and document.get("Функция") == "СЧФДОП"
    assert document.get("ДатаИнфПр") == "08.10.2026" and document.get("ВремИнфПр") == "14.05.09"
    # Порядок узлов задан схемой.
    assert [child.tag for child in document] == [
        "СвСчФакт", "ТаблСчФакт", "СвПродПер", "Подписант"]
    assert [child.tag for child in document.find("СвСчФакт")] == [
        "СвПрод", "ГрузОт", "ГрузПолуч", "ДокПодтвОтгрНом", "СвПокуп", "ДенИзм",
        "ИнфПолФХЖ1"]
    assert document.find("СвСчФакт").attrib == {"НомерДок": "ПР-15", "ДатаДок": "08.10.2026"}
    transfer = document.find("СвПродПер/СвПер")
    assert transfer.get("ДатаПер") == "08.10.2026"      # дата, на которую выбывают коды
    assert transfer.find("БезДокОснПер").text == "1"


def test_ип_пишется_фамилией_именем_и_отчеством_а_организация_названием_и_кпп():
    root = _root(_document())

    person = root.find("Документ/СвСчФакт/СвПрод/ИдСв/СвИП")
    assert person.get("ИННФЛ") == "254009895745"
    assert person.find("ФИО").attrib == {"Фамилия": "Саух", "Имя": "Мария",
                                         "Отчество": "Николаевна"}
    company = root.find("Документ/СвСчФакт/СвПокуп/ИдСв/СвЮЛУч")
    assert company.attrib == {"НаимОрг": 'ООО "Бьюти Глобал"', "ИННЮЛ": "7730306682",
                              "КПП": "773001001"}
    # Грузополучатель — тот же покупатель.
    assert root.find("Документ/СвСчФакт/ГрузПолуч/ИдСв/СвЮЛУч").get("ИННЮЛ") == "7730306682"


def test_коды_идут_по_строкам_товара_кодом_идентификации_без_хвоста():
    root = _root(_document())

    rows = root.findall("Документ/ТаблСчФакт/СведТов")
    assert [row.get("КолТов") for row in rows] == ["2", "1"]
    assert [row.find("ДопСведТов").get("ГТИН") for row in rows] == [GTIN, OTHER]
    first = [item.text for item in rows[0].findall("ДопСведТов/НомСредИдентТов/КИЗ")]
    assert first == [FIRST_KI, SECOND_KI]
    assert rows[0].get("ОКЕИ_Тов") == "796" and rows[0].get("НаимЕдИзм") == "шт"


def test_спецсимволы_кода_экранируются():
    code = f"01{GTIN}215a<b&c>d'\"e1"
    lines = tuple(sale.lines_from_codes([code], names={GTIN: "Духи"},
                                        prices={GTIN: Decimal("10")}))
    data = _document(lines=lines).render()

    assert b"215a&lt;b&amp;c&gt;d" in data
    [kiz] = ET.fromstring(data).iter("КИЗ")
    assert kiz.text == f"01{GTIN}215a<b&c>d'\"e1"


def test_файл_в_windows_1251_а_знаки_вне_её_ссылками():
    lines = _lines(names={GTIN: "Chloé Eau de Parfum", OTHER: "Вода «Л'Эр»"})
    data = _document(lines=lines).render()

    assert data.startswith(b'<?xml version="1.0" encoding="windows-1251"?>')
    assert "Вода «Л'Эр»".encode("cp1251") in data
    assert b"Chlo&#233;" in data
    assert ET.fromstring(data).find("Документ/ТаблСчФакт/СведТов").get("НаимТов") \
        == "Chloé Eau de Parfum"


def test_имя_файла_получатель_отправитель_дата_guid_и_признак_маркировки():
    document = _document()

    assert document.file_id == (
        "ON_NSCHFDOPPR_2BM-7730306682-773001001-2023_2LT-254009895745_20261008_"
        "11111111-2222-3333-4444-555555555555_0_1_0_0_0_00")
    assert document.file_name == document.file_id + ".xml"
    assert _root(document).get("ИдФайл") == document.file_id


def test_тот_же_документ_даёт_те_же_байты():
    assert _document().render() == _document().render()
    assert sale.Sale(number="1", date="2026-10-08", seller=SELLER, buyer=BUYER,
                     signer=SIGNER, lines=_lines(), vat="22%").guid \
        != sale.Sale(number="1", date="2026-10-08", seller=SELLER, buyer=BUYER,
                     signer=SIGNER, lines=_lines(), vat="22%").guid


# --- деньги -------------------------------------------------------------------------

def test_ндс_выделяется_из_суммы_строки_по_цене_с_ндс():
    root = _root(_document())

    first = root.find("Документ/ТаблСчФакт/СведТов")
    # 2 × 4990 = 9980, НДС 22/122 = 1799,67, без НДС 8180,33, за штуку 4090,17.
    assert first.get("СтТовУчНал") == "9980.00"
    assert first.find("СумНал/СумНал").text == "1799.67"
    assert first.get("СтТовБезНДС") == "8180.33" and first.get("ЦенаТов") == "4090.17"
    assert first.get("НалСт") == "22%"
    total = root.find("Документ/ТаблСчФакт/ВсегоОпл")
    assert total.get("СтТовУчНалВсего") == "13313.33"
    assert total.get("СтТовБезНДСВсего") == "10912.57"
    assert total.find("СумНалВсего/СумНал").text == "2400.76"
    assert total.get("КолНеттоВс") == "3"


def test_без_ндс_пишется_словами_а_сумма_не_делится():
    root = _root(_document(vat="без НДС"))

    first = root.find("Документ/ТаблСчФакт/СведТов")
    assert first.get("НалСт") == "без НДС" and first.find("СумНал/БезНДС").text == "без НДС"
    assert first.get("СтТовБезНДС") == first.get("СтТовУчНал") == "9980.00"
    assert root.find("Документ/ТаблСчФакт/ВсегоОпл/СумНалВсего/БезНДС") is not None


@pytest.mark.parametrize("text, expected", [
    ("1 250,50", Decimal("1250.50")), ("99", Decimal("99.00")),
    ("12,345", Decimal("12.35")), ("", None), ("рубль", None), ("NaN", None)])
def test_цена_читается_как_её_пишут_люди(text, expected):
    assert sale.money(text) == expected


# --- страна, декларация, подписант, основание ---------------------------------------

def test_страна_по_коду_и_по_названию_и_номер_декларации():
    lines = _lines(origins={GTIN: "франция", OTHER: "380"},
                   customs={GTIN: "10005030/290425/5113372"})
    rows = _root(_document(lines=lines)).findall("Документ/ТаблСчФакт/СведТов")

    assert rows[0].find("СвДТ").attrib == {"КодПроисх": "250",
                                           "НомерДТ": "10005030/290425/5113372"}
    assert rows[0].find("ДопСведТов/КрНаимСтрПр").text == "ФРАНЦИЯ"
    assert rows[1].find("СвДТ").attrib == {"КодПроисх": "380"}
    assert sale.country("Великобритания") == ("826", "СОЕДИНЕННОЕ КОРОЛЕВСТВО")
    assert sale.country("Монако") == ("", "МОНАКО")      # без кода, но с названием
    assert sale.country("999") == ("999", "")


def test_без_страны_и_декларации_узла_декларации_нет():
    row = _root(_document()).find("Документ/ТаблСчФакт/СведТов")
    assert row.find("СвДТ") is None and row.find("ДопСведТов/КрНаимСтрПр") is None


def test_подписант_по_мчд_подтверждает_полномочия_доверенностью():
    signer = sale.Signer("Иванов", "Евгений", "", "Менеджер",
                         "1a2b3c4d-1111-2222-3333-444455556666", "2026-01-15")
    node = _root(_document(signer=signer)).find("Документ/Подписант")

    assert node.get("СпосПодтПолном") == "3" and node.get("Должн") == "Менеджер"
    assert node.find("СвДоверЭл").attrib == {
        "НомДовер": "1a2b3c4d-1111-2222-3333-444455556666",
        "ДатаВыдДовер": "15.01.2026", "ИдСистХран": sale.POA_SYSTEM}
    own = _root(_document()).find("Документ/Подписант")
    assert own.get("СпосПодтПолном") == "1" and own.find("СвДоверЭл") is None
    # По этим полям «ЭДО Лайт» находит МЧД и прикладывает её к пакету.
    fields = {item.get("Идентиф"): item.get("Значен") for item in _root(
        _document(signer=signer)).iter("ТекстИнф")}
    assert fields == {"СвВыбытияМАРК": "1",
                      "МЧД": "1a2b3c4d-1111-2222-3333-444455556666",
                      "Сведения об информационной системе": sale.POA_SYSTEM}
    assert len(list(_root(_document()).iter("ТекстИнф"))) == 1


def test_договор_основание_вместо_без_основания():
    root = _root(_document(basis_name="Договор", basis_number="15/П",
                           basis_date="2026-10-01"))

    transfer = root.find("Документ/СвПродПер/СвПер")
    assert transfer.find("ОснПер").attrib == {"РеквНаимДок": "Договор",
                                              "РеквНомерДок": "15/П",
                                              "РеквДатаДок": "01.10.2026"}
    assert transfer.find("БезДокОснПер") is None


# --- что мешает собрать документ ----------------------------------------------------

@pytest.mark.parametrize("changes, expected", [
    (dict(vat=""), "ставка НДС"),
    (dict(number=" "), "номер документа"),
    (dict(buyer=sale.Party(inn="7730306682", name="ООО Р", address="М",
                           edo_id="2BM-1")), "КПП покупателя"),
    (dict(buyer=sale.Party(inn="7730306682", kpp="773001001", name="ООО Р",
                           address="М", edo_id="")), "идентификатор ЭДО покупателя"),
    (dict(seller=sale.Party(inn="254009895745", name="ИП", address="В",
                            edo_id="2LT-1")), "фамилия и имя"),
    (dict(buyer=SELLER), "одна и та же"),
    (dict(signer=sale.Signer("Саух", "")), "подписанта"),
    (dict(signer=sale.Signer("Саух", "Мария")), "должность"),
    (dict(signer=sale.Signer("И", "Е", "", "Менеджер", poa_number="123", poa_date="2026-01-01")),
     "номер МЧД"),
    (dict(lines=_lines(prices={GTIN: Decimal("0")})), "цена за штуку"),
    (dict(lines=_lines(names={GTIN: ""})), "нет наименования"),
    (dict(lines=_lines(origins={GTIN: "999"})), "код страны 999"),
    (dict(basis_number="15"), "основание"),
    (dict(lines=()), "нет ни одного товара"),
])
def test_негодный_документ_не_собирается_и_причина_названа(changes, expected):
    document = _document(**changes)

    assert any(expected in problem for problem in document.problems), document.problems
    with pytest.raises(sale.SaleProblem):
        document.render()


def test_одинаковые_экземпляры_с_разным_хвостом_это_дубль():
    twin = f"01{GTIN}215hsFUtaRDI3mf"
    lines = tuple(sale.lines_from_codes([FIRST, twin], names={GTIN: "Д"},
                                        prices={GTIN: Decimal("1")}))
    assert "в документе есть одинаковые коды" in _document(lines=lines).problems


def test_продавать_можно_только_свой_код_в_обороте():
    def info(code, **values):
        defaults = dict(code=code, gtin=GTIN, serial=code[-5:], found=True,
                        state=CodeState.INTRODUCED, owner_inn="254009895745")
        defaults.update(values)
        return CodeInfo(**defaults)

    problems = sale.code_problems([
        info(FIRST_KI),
        info(SECOND_KI, owner_inn="7700000000"),
        info(THIRD_KI, state=CodeState.RETIRED),
        info("0100000000000000215xxxxx", found=False),
    ], "254009895745")

    assert len(problems) == 3
    assert "другим участником" in problems[0] and "7700000000" in problems[0]
    assert "Выведен из оборота" in problems[1]
    assert "не знает" in problems[2]


# --- «ЭДО Лайт» ---------------------------------------------------------------------

@pytest.fixture
def served(monkeypatch):
    calls: list = []
    answers: list = []

    def send(prepared, tolerate=()):
        calls.append(prepared)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(transport, "_send", send)
    signed: list = []
    monkeypatch.setattr(edo.session, "authorized", lambda call: call("TOKEN"))
    monkeypatch.setattr(
        edo.crypto, "sign",
        lambda data, thumbprint, detached=False: signed.append((data, detached)) or "UE9EUA==")
    return calls, answers, signed


def _parts(request) -> dict[str, tuple[str, bytes]]:
    boundary = re.search(r"boundary=(\S+)", request.headers["Content-type"]).group(1)
    found = {}
    for chunk in request.data.split(b"--" + boundary.encode())[1:-1]:
        # Часть — «\r\n заголовки \r\n\r\n тело \r\n»: снимается ровно обёртка,
        # а свой перевод строки в конце файла остаётся при нём.
        head, _, body = chunk.removeprefix(b"\r\n").removesuffix(b"\r\n") \
            .partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
        filename = re.search(rb'filename="([^"]+)"', head)
        found[name] = (filename.group(1).decode() if filename else "", body)
    return found


def test_упд_уходит_файлом_с_его_именем_и_открепленной_подписью_этих_байтов(served):
    calls, answers, signed = served
    answers.append({"id": "aaaa-bbbb"})
    document = _document()

    doc_id = edo.send(document, "ААББ", Contour.PRODUCTION)

    assert doc_id == "aaaa-bbbb"
    request = calls[0]
    assert request.full_url == "https://edo-gismt.crpt.ru/api/v1/outgoing-documents"
    assert request.get_method() == "POST"
    assert request.headers["Authorization"] == "Bearer TOKEN"
    parts = _parts(request)
    assert parts["content"] == (document.file_name, document.render())
    assert parts["signature"] == ("", b"UE9EUA==")
    # Подписаны ровно байты файла, подпись открепленная.
    assert signed == [(document.render(), True)]
    assert "Send_mchd_file" not in request.headers


def test_черновик_уходит_без_подписи_и_без_сертификата(served):
    calls, answers, signed = served
    answers.append({"id": "draft-1"})
    document = _document()

    assert edo.send(document, "", draft=True) == "draft-1"

    parts = _parts(calls[0])
    assert set(parts) == {"content"} and parts["content"][1] == document.render()
    assert signed == []


def test_идентификатор_сбис_без_дефиса_принимается():
    buyer = sale.Party(inn="253601020702", name="ИП Хобта Юлия Николаевна",
                       address="г Владивосток",
                       edo_id="2BEE4616F14FC7C455CB30B828C396CE317")
    document = _document(buyer=buyer)

    assert document.problems == []
    assert document.file_id.startswith(
        "ON_NSCHFDOPPR_2BEE4616F14FC7C455CB30B828C396CE317_2LT-254009895745_")
    assert any("идентификатор ЭДО" in problem
               for problem in sale.Party(edo_id="2B").problems("покупателя"))


def test_подписанту_по_мчд_оператор_прикладывает_доверенность(served):
    calls, answers, _ = served
    answers.append({"id": "aaaa"})
    signer = sale.Signer("Иванов", "Евгений", "", "Менеджер",
                         poa_number="1a2b3c4d-1111-2222-3333-444455556666",
                         poa_date="2026-01-15")

    edo.send(_document(signer=signer), "ААББ")

    assert calls[0].headers["Send_mchd_file"] == "true"


def test_причина_отказа_эдо_лайт_показывается_словами(served):
    _, answers, _ = served
    answers.append(transport._failure(400, (
        '{"errors":[{"error_message":"Элемент Документ.Подписант.Должн обязателен '
        'при <Функция>=СЧФДОП | ДОП"}]}').encode("utf-8")))

    with pytest.raises(transport.MarkingError, match="Должн обязателен"):
        edo.send(_document(), "ААББ")


def test_обрыв_связи_не_повторяется_сам_и_называется_неизвестным_исходом(served):
    calls, answers, _ = served
    answers.append(transport.Offline("сети нет"))

    with pytest.raises(edo.EdoUnknown, match="ещё раз"):
        edo.send(_document(), "ААББ")

    assert len(calls) == 1


def test_без_сертификата_и_с_негодным_документом_ничего_не_уходит(served):
    calls, _, signed = served

    with pytest.raises(sale.SaleProblem, match="сертификат"):
        edo.send(_document(), " ")
    with pytest.raises(sale.SaleProblem):
        edo.send(_document(vat=""), "ААББ")

    assert calls == [] and signed == []


def _listing(*documents):
    return {"items": [{"id": doc_id, "status": code,
                       "documents": [{"id": doc_id, "status": code}]}
                      for doc_id, code in documents], "has_next_page": False}


def test_статус_берётся_из_списка_исходящих(served):
    calls, answers, _ = served
    answers.append(_listing(("другой", 61), ("aaaa", 3)))

    current = edo.status("aaaa", Contour.SANDBOX, since=datetime(2026, 10, 8, 12, 0))

    assert current.code == 3 and current.pending
    assert current.title == "Доставлен, ждёт подписи покупателя"
    url = calls[0].full_url
    assert url.startswith("https://edo.sandbox.crptech.ru/api/v1/outgoing-documents?")
    assert "created_from=" in url and "folder=0" in url


def test_документ_не_найденный_в_списке_не_выдаётся_за_итог(served):
    _, answers, _ = served
    answers.append(_listing(("другой", 61)))

    current = edo.status("aaaa")

    assert not current.found and current.pending and "не найден" in current.title


def test_незнакомый_ответ_списка_не_выдаётся_за_пустой(served):
    _, answers, _ = served
    answers.append({"что-то": []})

    with pytest.raises(transport.MarkingError, match="не так"):
        edo.status("aaaa")


def _infos(state):
    return [CodeInfo(code=code, found=True, state=state)
            for code in (FIRST_KI, SECOND_KI)]


@pytest.mark.parametrize("code, state, expected, withdrawn", [
    (3, None, OperationStatus.SENT, False),
    (61, CodeState.RETIRED, OperationStatus.DONE, True),
    (61, CodeState.INTRODUCED, OperationStatus.SENT, False),
    (5, None, OperationStatus.REJECTED, False),
    (62, None, OperationStatus.REJECTED, False),
])
def test_выполненной_продажа_считается_когда_коды_выбыли(served, code, state,
                                                        expected, withdrawn):
    _, answers, _ = served
    answers.append(_listing(("aaaa", code)))
    asked: list = []

    def check(codes, contour, use_cache):
        asked.append((codes, use_cache))
        return _infos(state)

    outcome = edo.follow("aaaa", [FIRST_KI, SECOND_KI], check=check)

    assert outcome.operation_status is expected and outcome.withdrawn is withdrawn
    # Коды спрашиваются только у переданного в ГИС МТ, и спрашиваются свежими.
    assert asked == ([([FIRST_KI, SECOND_KI], False)] if code == 61 else [])
    if code == 61 and not withdrawn:
        assert "ещё обрабатывает" in outcome.text


# --- вкладка ------------------------------------------------------------------------

@pytest.fixture(scope="module")
def application():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def world(application, tmp_path, monkeypatch):
    """Вкладка с подменённым входом, проверкой, отправкой и окном подтверждения."""
    from PySide6.QtWidgets import QMessageBox

    from app.core.marking import service
    from app.core.settings import AppSettings
    from app.ui.widgets import sale_tab as module

    state = {"signed": True, "yes": True, "text": [], "sent": [], "answer": None,
             "infos": None, "checked": [], "thumbprint": "ААББ",
             "contour": Contour.SANDBOX, "told": [], "inn": "254009895745"}

    class Session:
        contour = Contour.SANDBOX

        @property
        def inn(self):
            return state["inn"]

    class Fake:
        Icon = QMessageBox.Icon
        ButtonRole = QMessageBox.ButtonRole

        def __init__(self, parent=None):
            self._yes = None

        def setWindowTitle(self, text):
            pass

        def setIcon(self, icon):
            pass

        def setText(self, text):
            state["text"].append(text)

        def setInformativeText(self, text):
            state["text"].append(text)

        def addButton(self, text, role):
            button = object()
            if role == QMessageBox.ButtonRole.DestructiveRole:
                self._yes = button
            return button

        def setDefaultButton(self, button):
            pass

        def exec(self):
            return 0

        def clickedButton(self):
            return self._yes if state["yes"] else None

    def check(identifiers, contour):
        state["checked"].append(list(identifiers))
        if state["infos"] is not None:
            return state["infos"]
        return [CodeInfo(code=code, gtin=code[2:16], serial=code[18:], found=True,
                         state=CodeState.INTRODUCED, owner_inn="254009895745",
                         product_name=f"Товар {code[2:16]}")
                for code in identifiers]

    def send(document, thumbprint, contour):
        state["sent"].append((document, thumbprint, contour))
        answer = state["answer"]
        if isinstance(answer, Exception):
            raise answer
        return answer or ("sent", "edo-1")

    monkeypatch.setattr(module, "QMessageBox", Fake)
    monkeypatch.setattr(module, "_check", check)
    monkeypatch.setattr(module, "_send", send)
    monkeypatch.setattr(service, "signed_in", lambda: state["signed"])
    monkeypatch.setattr(service, "current", lambda: Session())
    settings = AppSettings(_path=tmp_path / "settings.json")
    settings.marking_sale_seller = {
        "inn": "254009895745", "name": "Саух Мария Николаевна",
        "address": "г Владивосток", "edo_id": "2LT-254009895745",
        "surname": "Саух", "first_name": "Мария", "patronymic": "Николаевна",
        "position": "Индивидуальный предприниматель"}
    tab = module.SaleTab(
        settings, notify=lambda text, kind: state["told"].append((text, kind)),
        contour=lambda: state["contour"], thumbprint=lambda: state["thumbprint"],
        inn=lambda: "254009895745")
    tab.buyer_inn_edit.setText("7730306682")
    tab.buyer_kpp_edit.setText("773001001")
    tab.buyer_name_edit.setText("ООО Бьюти Глобал")
    tab.buyer_address_edit.setText("г Москва")
    tab.buyer_edo_edit.setText("2BM-7730306682")
    tab.number_edit.setText("ПР-1")
    tab.vat_box.setCurrentIndex(tab.vat_box.findData("22%"))
    return tab, state


def _settle(application) -> None:
    from PySide6.QtCore import QThreadPool

    for _ in range(6):
        QThreadPool.globalInstance().waitForDone(3000)
        application.processEvents()


def _price(tab, row: int, text: str) -> None:
    from app.ui.widgets.sale_tab import PRICE

    tab.table.item(row, PRICE).setText(text)


def test_сканы_складываются_в_строки_по_товару(world):
    tab, state = world

    assert tab.add_code(FIRST) == "added"
    assert tab.add_code(f"01{GTIN}215hsFUtaRDI3mf") == "repeat"   # тот же экземпляр
    assert tab.add_code("мусор") == "broken"
    tab.add_many(f"{SECOND}\n{THIRD}")

    assert tab.table.rowCount() == 2
    assert [tab.table.item(row, 0).text() for row in range(2)] == [GTIN, OTHER]
    assert [tab.table.item(row, 2).text() for row in range(2)] == ["2", "1"]
    tab.undo_scan()
    assert tab.table.rowCount() == 1 and tab.codes == [FIRST, SECOND]


def test_цена_пересчитывает_сумму_и_итог(world):
    tab, _ = world
    tab.add_many(f"{FIRST}\n{SECOND}")

    _price(tab, 0, "4 990,00")

    from app.ui.widgets.sale_tab import SUM
    assert tab.table.item(0, SUM).text() == "9 980,00"
    assert "9 980,00 ₽" in tab.total_label.text()
    assert "НДС 22% — 1 799,67" in tab.total_label.text()


def test_проверка_кодов_подставляет_названия_из_честного_знака(application, world):
    tab, state = world
    tab.add_many(f"{FIRST}\n{THIRD}")

    tab.check_codes()
    _settle(application)

    assert state["checked"] == [[FIRST_KI, THIRD_KI]]
    assert tab.table.item(0, 1).text() == f"Товар {GTIN}"
    assert "можно продавать" in tab.table.item(0, 7).text()
    assert state["sent"] == []                       # проверка ничего не отправляет


def test_без_названий_и_цен_отправлять_нечего(world):
    tab, state = world
    tab.add_many(FIRST)

    tab.send_document()

    assert "нет наименования" in state["told"][-1][0]
    assert "Проверить коды" in state["told"][-1][0]
    assert state["checked"] == [] and state["sent"] == []


def _ready(tab):
    tab.add_many(f"{FIRST}\n{SECOND}\n{THIRD}")
    tab.table.item(0, 1).setText("Парфюм")
    tab.table.item(1, 1).setText("Вода")
    _price(tab, 0, "4990")
    _price(tab, 1, "3333,33")


@pytest.mark.parametrize("break_it, expected", [
    (lambda tab, state: state.update(signed=False), "войдите"),
    (lambda tab, state: state.update(thumbprint=""), "выберите сертификат"),
    (lambda tab, state: state.update(contour=Contour.PRODUCTION), "другом контуре"),
    (lambda tab, state: state.update(inn="7700000000"), "под кем"),
    (lambda tab, state: tab.vat_box.setCurrentIndex(0), "ставка НДС"),
])
def test_без_условий_отправки_ничего_не_спрашивается_и_не_уходит(world, break_it, expected):
    from app.core.marking import service

    tab, state = world
    _ready(tab)
    break_it(tab, state)

    tab.send_document()

    assert expected in state["told"][-1][0]
    assert state["checked"] == [] and state["sent"] == [] and state["text"] == []
    assert service.history() == []


def test_чужие_или_выбывшие_коды_останавливают_продажу_до_вопроса(application, world):
    from app.core.marking import service

    tab, state = world
    _ready(tab)
    state["infos"] = [
        CodeInfo(code=FIRST_KI, gtin=GTIN, serial="5hsFUtaRDI3mf", found=True,
                 state=CodeState.INTRODUCED, owner_inn="254009895745"),
        CodeInfo(code=SECOND_KI, gtin=GTIN, serial="5abcDEFxyZ01", found=True,
                 state=CodeState.RETIRED, owner_inn="254009895745"),
        CodeInfo(code=THIRD_KI, gtin=OTHER, serial="5QwErTyUiOp12", found=True,
                 state=CodeState.INTRODUCED, owner_inn="7700000000"),
    ]

    tab.send_document()
    _settle(application)

    assert state["text"] == [] and state["sent"] == [] and service.history() == []
    assert "Продать нельзя" in tab.hint.text() and "Выведен из оборота" in tab.hint.text()
    assert "нельзя продать" in tab.table.item(0, 7).text()


def test_отказ_в_подтверждении_ничего_не_отправляет(application, world):
    from app.core.marking import service

    tab, state = world
    state["yes"] = False
    _ready(tab)

    tab.send_document()
    _settle(application)

    assert state["sent"] == [] and service.history() == []
    text = "\n".join(state["text"])
    assert "ООО Бьюти Глобал" in text and "3 шт." in text and "13 313,33" in text
    assert "собственных нужд" in text and "аннулированием" in text


def test_отправленный_упд_пишется_в_журнал_запоминается_и_очищает_список(application, world):
    from app.core.marking import service

    tab, state = world
    _ready(tab)
    changed: list = []
    tab.journal_changed.connect(lambda: changed.append(1))

    tab.send_document()
    _settle(application)

    [(document, thumbprint, contour)] = state["sent"]
    assert thumbprint == "ААББ" and contour is Contour.SANDBOX
    assert document.buyer.inn == "7730306682" and document.vat == "22%"
    assert document.identifiers == [FIRST_KI, SECOND_KI, THIRD_KI]
    [operation] = service.history()
    assert operation.kind is OperationKind.SHIP and operation.status is OperationStatus.SENT
    assert operation.document_id == "edo-1" and operation.codes == [FIRST_KI, SECOND_KI, THIRD_KI]
    assert "УПД № ПР-1 от" in operation.comment and "Бьюти Глобал" in operation.comment
    assert changed and tab.codes == [] and tab.number_edit.text() == "ПР-2"
    # Покупатель и товары запомнены на следующую продажу.
    settings = tab.settings
    assert settings.marking_sale_buyers["7730306682"]["edo_id"] == "2BM-7730306682"
    assert settings.marking_sale_items[GTIN] == {"name": "Парфюм", "price": "4990.00",
                                                 "origin": "", "customs": ""}
    assert settings.marking_sale_seller["last_number"] == "ПР-1"
    assert tab.sent_table.rowCount() == 1


def test_запомненное_подставляется_при_следующей_продаже(world):
    tab, _ = world
    tab.settings.marking_sale_items[GTIN] = {"name": "Парфюм", "price": "4990",
                                             "origin": "250", "customs": ""}
    tab.settings.marking_sale_buyers["7701234567"] = {
        "name": "ООО Ромашка", "kpp": "770101001", "address": "Москва",
        "edo_id": "2BM-7701234567"}

    tab.add_code(FIRST)
    for edit in (tab.buyer_kpp_edit, tab.buyer_name_edit, tab.buyer_address_edit,
                 tab.buyer_edo_edit):
        edit.clear()
    tab.buyer_inn_edit.setText("7701234567")
    tab.buyer_inn_edit.editingFinished.emit()

    [line] = tab.lines()
    assert line.name == "Парфюм" and line.price == Decimal("4990.00") and line.origin == "250"
    assert tab.buyer_name_edit.text() == "ООО Ромашка"
    assert tab.buyer_edo_edit.text() == "2BM-7701234567"


def test_неизвестный_исход_повторяется_тем_же_файлом(application, world):
    from app.core.marking import service

    tab, state = world
    _ready(tab)
    state["answer"] = ("unknown", "Связь оборвалась")

    tab.send_document()
    _settle(application)

    assert tab.codes and tab.send_button.text() == "Отправить повторно"
    [operation] = service.history()
    assert operation.status is OperationStatus.SENT and operation.error == "исход неизвестен"

    state["answer"] = None
    tab.send_document()
    _settle(application)

    first, second = state["sent"][0][0], state["sent"][1][0]
    assert first.file_name == second.file_name            # оператор узнает тот же файл
    assert "повтор того же документа" in "\n".join(state["text"])
    [operation] = service.history()                      # и запись в журнале та же
    assert operation.document_id == "edo-1"


def test_ошибка_отправки_помечает_операцию_неотправленной_и_оставляет_коды(application, world):
    from app.core.marking import service

    tab, state = world
    _ready(tab)
    state["answer"] = transport.MarkingError("Нет прав на подписание документа")

    tab.send_document()
    _settle(application)

    [operation] = service.history()
    assert operation.status is OperationStatus.FAILED
    assert "Нет прав" in operation.error and tab.codes


def test_обновление_статусов_переводит_продажу_в_выполненные(application, world, monkeypatch):
    from app.core.marking import service
    from app.ui.widgets import sale_tab as module

    tab, state = world
    _ready(tab)
    tab.send_document()
    _settle(application)
    asked: list = []

    def follow(doc_id, codes, contour, since=None):
        asked.append((doc_id, list(codes), contour))
        return edo.Outcome(edo.EdoStatus(61), retired=len(codes), total=len(codes))

    monkeypatch.setattr(module.edo, "follow", follow)

    tab.refresh_statuses()
    _settle(application)

    assert asked == [("edo-1", [FIRST_KI, SECOND_KI, THIRD_KI], Contour.SANDBOX)]
    [operation] = service.history()
    assert operation.status is OperationStatus.DONE
    assert "коды выведены из оборота (3)" in operation.error
    assert "выведены" in tab.sent_table.item(0, 4).text()


DIADOC = "2BM-254009895745-20200424111103944571600000000"


@pytest.fixture
def saved(world, tmp_path, monkeypatch):
    """Окно «Сохранить как» отвечает предложенным именем в папке теста."""
    from app.ui.widgets import sale_tab as module

    tab, state = world
    tab.settings.marking_sale_seller["diadoc_id"] = DIADOC
    state["asked"] = []

    def ask(parent, title, name, filters):
        state["asked"].append(name)
        return str(tmp_path / name), filters

    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", ask)
    return tab, state


def test_упд_для_диадока_проверяет_коды_сохраняет_файл_и_пишет_журнал(application, saved,
                                                                     tmp_path):
    from app.core.marking import service

    tab, state = saved
    _ready(tab)

    tab.save_for_diadoc()
    _settle(application)

    assert state["checked"] == [[FIRST_KI, SECOND_KI, THIRD_KI]]
    [name] = state["asked"]
    # Отправитель — ваш ящик в Диадоке, а не в «ЭДО Лайт».
    assert re.fullmatch(rf"ON_NSCHFDOPPR_2BM-7730306682_{DIADOC}_\d{{8}}_"
                        r"[0-9a-f-]{36}_0_1_0_0_0_00\.xml", name)
    data = (tmp_path / name).read_bytes()
    assert ET.fromstring(data).get("ИдФайл") + ".xml" == name
    assert "СвВыбытияМАРК".encode("cp1251") in data
    [operation] = service.history()
    assert operation.kind is OperationKind.SHIP and operation.status is OperationStatus.SENT
    assert operation.document_id.startswith(edo.MANUAL_PREFIX)
    assert operation.comment.endswith("· Диадок") and "ждём подписи" in operation.error
    assert state["sent"] == [] and tab.codes == [] and tab.number_edit.text() == "ПР-2"
    assert "Загрузите его в Диадок" in state["told"][-1][0]


def test_для_диадока_чужие_коды_не_сохраняются(application, saved):
    from app.core.marking import service

    tab, state = saved
    _ready(tab)
    state["infos"] = [CodeInfo(code=code, found=True, state=CodeState.RETIRED,
                               owner_inn="254009895745")
                      for code in (FIRST_KI, SECOND_KI, THIRD_KI)]

    tab.save_for_diadoc()
    _settle(application)

    assert state["asked"] == [] and service.history() == [] and tab.codes
    assert "Продать нельзя" in tab.hint.text()


def test_без_id_в_диадоке_файл_не_собирается(application, saved):
    tab, state = saved
    _ready(tab)
    del tab.settings.marking_sale_seller["diadoc_id"]

    tab.save_for_diadoc()

    assert "идентификатор ЭДО продавца" in state["told"][-1][0]
    assert state["checked"] == [] and state["asked"] == []


def test_упд_из_диадока_выполнен_когда_выбыли_все_коды():
    def check(state):
        return lambda codes, contour, use_cache: [
            CodeInfo(code=code, found=True, state=state) for code in codes]

    doc_id = edo.MANUAL_PREFIX + "ON_NSCHFDOPPR_x"
    waiting = edo.follow(doc_id, [FIRST_KI, SECOND_KI], check=check(CodeState.INTRODUCED))
    done = edo.follow(doc_id, [FIRST_KI, SECOND_KI], check=check(CodeState.RETIRED))

    assert waiting.operation_status is OperationStatus.SENT
    assert "Диадок" in waiting.text and "ждём подписи" in waiting.text
    assert done.operation_status is OperationStatus.DONE
    assert done.text == "Диадок: коды выведены из оборота (2)"


def test_реквизиты_продавца_нужен_хотя_бы_один_id_эдо():
    from app.ui.widgets.sale_tab import seller_problems

    values = {"inn": "254009895745", "name": "Саух Мария Николаевна", "address": "В",
              "surname": "П", "first_name": "Н", "position": "Начальник склада"}

    assert any("идентификатор ЭДО" in item for item in seller_problems(values))
    assert seller_problems(values | {"diadoc_id": DIADOC}) == []
    assert seller_problems(values | {"edo_id": "2LT-11000001578"}) == []
    assert any("идентификатор ЭДО" in item
               for item in seller_problems(values | {"diadoc_id": DIADOC, "edo_id": "x"}))


def test_вкладка_стоит_в_маркировке(application, tmp_path):
    from app.core.settings import AppSettings
    from app.ui.marking_page import SALE_TAB, MarkingPage

    page = MarkingPage(AppSettings(_path=tmp_path / "settings.json"), lambda *_: None)

    assert page.tabs.tabText(SALE_TAB) == "Продажа"
    assert page.tabs.widget(SALE_TAB) is page.sale_tab
    page.tabs.setCurrentIndex(SALE_TAB)
    assert page.sale_tab.sent_table.rowCount() == 0


def test_реквизиты_продажи_переживают_перезапуск(tmp_path):
    from app.core.settings import AppSettings

    settings = AppSettings(_path=tmp_path / "settings.json")
    settings.marking_sale_seller = {"inn": "254009895745", "vat": "22%"}
    settings.marking_sale_buyers = {"7730306682": {"name": "ООО Р", "edo_id": "2BM-1"}}
    settings.marking_sale_items = {GTIN: {"price": "4990.00"}}
    settings.save()

    again = AppSettings.load(str(tmp_path / "settings.json"))

    assert again.marking_sale_seller == {"inn": "254009895745", "vat": "22%"}
    assert again.marking_sale_buyers["7730306682"]["edo_id"] == "2BM-1"
    assert again.marking_sale_items[GTIN]["price"] == "4990.00"


def test_номер_следующего_документа():
    from app.ui.widgets.sale_tab import next_number

    assert next_number("ПР-15") == "ПР-16"
    assert next_number("0099") == "0100"
    assert next_number("15/10-2") == "15/10-3"
    assert next_number("без цифр") == ""


# --- товар без марок ----------------------------------------------------------------

def _bare(**changes) -> sale.Line:
    values = dict(gtin=OTHER, name="Шампунь 300 мл", price=Decimal("990.00"), codes=(),
                  count=3)
    values.update(changes)
    return sale.Line(**values)


def test_товар_без_марок_идёт_без_кодов_и_без_gtin_а_вывода_из_оборота_нет():
    document = _document(lines=(_bare(),))
    root = _root(document)

    [row] = root.findall("Документ/ТаблСчФакт/СведТов")
    assert row.get("КолТов") == "3" and row.get("СтТовУчНал") == "2970.00"
    extra = row.find("ДопСведТов")
    assert extra.get("ПрТовРаб") == "1" and extra.get("ГТИН") is None
    assert extra.find("НомСредИдентТов") is None
    # Выводить нечего — признака нет, и в имени файла снят признак маркировки.
    assert root.find("Документ/СвСчФакт/ИнфПолФХЖ1") is None
    assert document.file_id.endswith("_0_0_0_0_0_00") and document.identifiers == []


def test_в_смешанном_упд_коды_только_у_маркированных_строк():
    document = _document(lines=(*_lines()[:1], _bare(gtin="", origin="Израиль")))
    root = _root(document)

    first, second = root.findall("Документ/ТаблСчФакт/СведТов")
    assert [item.text for item in first.findall("ДопСведТов/НомСредИдентТов/КИЗ")] == [
        FIRST_KI, SECOND_KI]
    assert second.find("ДопСведТов/НомСредИдентТов") is None
    assert second.find("ДопСведТов/КрНаимСтрПр").text == "ИЗРАИЛЬ"
    [info] = root.findall("Документ/СвСчФакт/ИнфПолФХЖ1/ТекстИнф")
    assert info.get("Идентиф") == "СвВыбытияМАРК"
    assert document.file_id.endswith("_0_1_0_0_0_00") and document.quantity == 5


def test_у_товара_без_марок_нужно_количество():
    document = _document(lines=(_bare(count=0, name="", gtin=""),))

    assert any("строка 1: количество" in item for item in document.problems)


def test_без_марок_коды_не_проверяются_а_количество_правится(application, world):
    from app.ui.widgets.sale_tab import CHECK, QUANTITY

    tab, state = world
    _ready(tab)
    tab.table.selectRow(1)

    tab.toggle_unmarked()

    assert tab.table.item(1, CHECK).text() == "без марок"
    assert tab.unmark_button.text() == "С марками"
    assert tab.table.item(1, QUANTITY).flags() & Qt_editable()
    assert not tab.table.item(0, QUANTITY).flags() & Qt_editable()
    tab.table.item(1, QUANTITY).setText("4")
    assert "из них без марок 4" in tab.total_label.text()
    tab.check_codes()
    _settle(application)
    assert state["checked"] == [[FIRST_KI, SECOND_KI]]

    tab.table.selectRow(1)
    tab.toggle_unmarked()
    [_, line] = tab.lines()
    assert line.codes == (THIRD,) and line.quantity == 1


def Qt_editable():
    from PySide6.QtCore import Qt

    return Qt.ItemFlag.ItemIsEditable


def test_чужие_марки_продаются_строкой_без_марок(application, world):
    from app.core.marking import service

    tab, state = world
    _ready(tab)
    state["infos"] = [CodeInfo(code=code, gtin=code[2:16], found=True,
                               state=CodeState.INTRODUCED, owner_inn="7700000000")
                      for code in (FIRST_KI, SECOND_KI, THIRD_KI)]

    tab.send_document()
    _settle(application)
    assert "«Без марок»" in tab.hint.text() and state["sent"] == []

    tab.table.selectAll()
    tab.toggle_unmarked()
    state["checked"].clear()
    tab.send_document()
    _settle(application)

    [(document, _, _)] = state["sent"]
    assert document.identifiers == [] and document.quantity == 3
    assert state["checked"] == []
    text = "\n".join(state["text"])
    assert "Без марок: 3 шт." in text and "выбудут" not in text
    # Журнал — о кодах; УПД без них уходит без записи, а форма очищается.
    assert service.history() == [] and tab.codes == [] and tab.number_edit.text() == "ПР-2"


def test_товар_без_марки_добавляется_руками(application, world):
    from app.ui.widgets.sale_tab import CHECK, NAME, PRICE, QUANTITY

    tab, _ = world
    tab.add_code(FIRST)
    tab.add_unmarked()

    assert tab.table.rowCount() == 2 and tab.table.item(1, 0).text() == ""
    tab.table.item(1, NAME).setText("Кондиционер 300 мл")
    tab.table.item(1, QUANTITY).setText("2")
    tab.table.item(1, PRICE).setText("1250,50")
    assert tab.table.item(1, CHECK).text() == "без марок"
    [marked, bare] = tab.lines()
    assert marked.marked and not bare.marked
    assert (bare.name, bare.quantity, bare.price) == ("Кондиционер 300 мл", 2,
                                                       Decimal("1250.50"))

    tab.table.selectRow(1)
    tab.remove_selected()
    assert tab.table.rowCount() == 1 and tab.codes == [FIRST]


def test_упд_без_марок_для_диадока_сохраняется_без_входа(application, saved, tmp_path):
    from app.core.marking import service
    from app.ui.widgets.sale_tab import NAME, PRICE

    tab, state = saved
    state["signed"] = False
    tab.add_unmarked()
    tab.table.item(0, NAME).setText("Мыло")
    tab.table.item(0, PRICE).setText("500")

    tab.save_for_diadoc()
    _settle(application)

    [name] = state["asked"]
    assert name.endswith("_0_0_0_0_0_00.xml") and state["checked"] == []
    data = (tmp_path / name).read_bytes()
    assert "СвВыбытияМАРК".encode("cp1251") not in data
    assert service.history() == [] and tab.number_edit.text() == "ПР-2"
    assert "Загрузите его в Диадок" in state["told"][-1][0]
    assert "выбудут" not in state["told"][-1][0]


def test_в_смешанном_упд_журнал_следит_только_за_кодами(application, saved):
    from app.core.marking import service

    tab, state = saved
    _ready(tab)
    tab.table.selectRow(1)
    tab.toggle_unmarked()

    tab.save_for_diadoc()
    _settle(application)

    [operation] = service.history()
    assert operation.codes == [FIRST_KI, SECOND_KI]
    assert "Коды выбудут" in state["told"][-1][0]


def test_повтор_после_правки_уходит_новым_файлом(application, world):
    tab, state = world
    _ready(tab)
    state["answer"] = ("unknown", "Связь оборвалась")
    tab.send_document()
    _settle(application)

    state["answer"] = None
    _price(tab, 0, "5000")
    tab.send_document()
    _settle(application)

    first, second = state["sent"][0][0], state["sent"][1][0]
    assert first.file_name != second.file_name


def test_редактор_ячейки_без_отступов_поля_формы(application, world):
    from PySide6.QtWidgets import QStyleOptionViewItem

    from app.ui.widgets.sale_tab import QUANTITY

    tab, _ = world
    tab.add_unmarked()
    delegate = tab.table.itemDelegate()
    index = tab.table.model().index(0, QUANTITY)

    editor = delegate.createEditor(tab.table.viewport(), QStyleOptionViewItem(), index)

    assert "padding: 0 7px" in editor.styleSheet()
    assert editor.validator() is not None


def test_скан_в_русской_раскладке_принимается(world):
    tab, _ = world

    assert tab.add_code("0102901449269226215Щ904(ЦШюЖШИИ") == "added"

    assert tab.codes == ["0102901449269226215O904(WI.:IBB"]
    assert tab.add_code("0102901449269226215O904(WI.:IBB") == "repeat"
