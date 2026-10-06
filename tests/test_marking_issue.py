"""Получение кодов из СУЗ, печать и ввод в оборот: ядро.

Сеть подменяется, профиль пользователя уводится во временную папку. Главное, что
проверяется, — сохранность: коды выдаются безвозвратно, и их запись на диск
должна случиться до того, как кто-либо ещё успеет с ними что-нибудь сделать.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from app.core.marking import datamatrix, introduce, issue, labels, transport
from app.core.marking.codes import GS
from app.core.marking.models import Contour
from app.core.marking.suz import Credentials

OMS = "b1a2c3d4-0000-1111-2222-333344445555"
ORDER = "b024ae09-ef7c-449e-b461-05d8eb116c79"
GTIN = "04607177964080"
CREDS = Credentials(oms_id=OMS, token="token-of-the-device", connection_id="c")

# Код с криптохвостом, как его выдаёт СУЗ для лёгкой промышленности.
CODE = f"01{GTIN}21Xy7Q2aB9pLm4{GS}91EE11{GS}92dGVzdGRhdGFmb3JsYWJlbHByZXZpZXd0ZXN0ZGF0YQ=="


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    transport.limiter.reset()
    return tmp_path


@pytest.fixture
def served(monkeypatch):
    """Подменённая отправка: что запросили и что ответить."""
    calls: list = []
    answers: list = []

    def send(prepared, tolerate=()):
        calls.append(prepared)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(transport, "_send", send)
    return calls, answers


# --- DataMatrix ---------------------------------------------------------------------

def test_эталонный_символ_не_меняется():
    """Символ сверен с декодером libdmtx; отпечаток ловит случайную порчу.

    Если кодировщик изменится и отпечаток уедет, символ надо снова прогнать
    через настоящий декодер — на сканере сломанный код не отличить глазами.
    """
    matrix = datamatrix.encode(CODE)
    text = "\n".join("".join("#" if cell else "." for cell in row) for row in matrix)

    assert len(matrix) == 36
    assert hashlib.sha1(text.encode()).hexdigest() ==         "7f4219e1f0e5d2580e5f25ba80894e30c704b7e4"


def test_поисковый_шаблон_символа():
    matrix = datamatrix.encode(CODE)
    side = len(matrix)

    # Сплошные левая и нижняя стороны, пунктир сверху и справа.
    assert all(matrix[row][0] for row in range(side))
    assert all(matrix[side - 1][col] for col in range(side))
    assert all(matrix[0][col] == (col % 2 == 0) for col in range(side))


def test_размер_растёт_вместе_с_кодом():
    short = datamatrix.side_for("0104600000000007" "21abc")
    long = datamatrix.side_for(CODE)

    assert short < long
    assert long <= 36


def test_код_без_криптохвоста_кодируется():
    assert len(datamatrix.encode(f"01{GTIN}21AbCdEfGhIjKlM")) >= 22


def test_пустой_и_слишком_длинный_код_отвергаются():
    with pytest.raises(datamatrix.DataMatrixError):
        datamatrix.encode("")
    with pytest.raises(datamatrix.DataMatrixError):
        datamatrix.encode("A" * 600)


def test_признак_символики_в_начале_не_попадает_в_данные():
    assert datamatrix.encode("]d2" + CODE) == datamatrix.encode(CODE)


# --- получение кодов ---------------------------------------------------------------

def test_коды_лежат_на_диске_до_возврата_из_получения(served):
    calls, answers = served
    answers.append({"omsId": OMS, "codes": [CODE, CODE.replace("Xy7Q", "Zz7Q")],
                    "blockId": "012cc7b0-c9e4-4511-8058-2de1f97a87b0"})

    batch = issue.fetch(CREDS, ORDER, GTIN, 2, Contour.SANDBOX,
                        product=issue.Product(gtin=GTIN, name="Колготки", tnved="6115"),
                        product_group="lp", release_method="REMAINS")

    assert os.path.exists(os.path.join(issue.folder(), f"{batch.id}.json"))
    again = issue.load(batch.id)
    assert again.codes == batch.codes and again.total == 2
    assert (again.name, again.tnved, again.release_method) == ("Колготки", "6115", "REMAINS")
    # Идентификатор блока нужен для восстановления.
    assert again.block_id.startswith("012cc7b0")
    assert "/api/v3/codes" in calls[0].full_url
    assert f"quantity=2" in calls[0].full_url and f"gtin={GTIN}" in calls[0].full_url


def test_токен_уходит_заголовком_клиентского_токена(served):
    calls, answers = served
    answers.append({"codes": [CODE], "blockId": "b"})

    issue.fetch(CREDS, ORDER, GTIN, 1)

    assert calls[0].get_header("Clienttoken") == "token-of-the-device"


def test_оборванная_связь_не_повторяет_получение(served, monkeypatch):
    """Повтор забрал бы второй блок — а потерянный возвращается отдельно."""
    calls, answers = served
    answers.append(transport.Offline("сети нет"))

    with pytest.raises(issue.CodesUnknown, match="Восстановить"):
        issue.fetch(CREDS, ORDER, GTIN, 1)

    assert len(calls) == 1
    assert issue.saved() == []


def test_пустой_ответ_не_выдаётся_за_успех(served):
    _, answers = served
    answers.append({"codes": [], "blockId": "b"})

    with pytest.raises(transport.MarkingError, match="кодов в ответе нет"):
        issue.fetch(CREDS, ORDER, GTIN, 1)


@pytest.mark.parametrize("quantity", [0, -3, issue.MAX_PER_REQUEST + 1])
def test_количество_проверяется_до_сети(served, quantity):
    calls, _ = served

    with pytest.raises(transport.MarkingError, match="от 1"):
        issue.fetch(CREDS, ORDER, GTIN, quantity)
    assert calls == []


def test_восстановление_берёт_только_отсутствующие_блоки(served):
    calls, answers = served
    answers.append({"codes": [CODE], "blockId": "known-block"})
    issue.fetch(CREDS, ORDER, GTIN, 1)
    answers.extend([
        {"blocks": [{"blockId": "known-block", "quantity": 1},
                    {"blockId": "lost-block", "quantity": 1}]},
        {"codes": [CODE.replace("Xy7Q", "Qq7Q")], "blockId": "lost-block"},
    ])

    restored = issue.recover(CREDS, ORDER, GTIN)

    assert [batch.block_id for batch in restored] == ["lost-block"]
    assert len(issue.saved()) == 2
    # Известный блок повторно не запрашивался.
    assert sum("order/codes/retry" in call.full_url for call in calls) == 1


def test_сведения_о_товаре_читаются_из_атрибутов_заказа(served):
    _, answers = served
    answers.append({GTIN: {"name": "Колготки", "tnVedCode": "6115",
                           "tnVedCode10": "6115950000"}})

    found = issue.product_info(CREDS, ORDER)

    assert found[GTIN].name == "Колготки"
    # Предпочтителен десятизначный код.
    assert found[GTIN].tnved == "6115950000"


def test_отказ_справочника_товара_не_мешает_получить_коды(served):
    _, answers = served
    answers.append(transport.MarkingError("Заказ не найден", 400))

    assert issue.product_info(CREDS, ORDER) == {}


def test_прогресс_печати_не_откатывается():
    batch = issue.Batch(id="b", gtin=GTIN, codes=[CODE] * 5)
    issue.save(batch)

    issue.mark_printed(batch, 4)
    issue.mark_printed(batch, 2)

    assert issue.load("b").printed == 4
    assert batch.left == 1
    issue.mark_printed(batch, 99)
    assert issue.load("b").printed == 5


def test_повреждённый_файл_блока_пропускается():
    issue.save(issue.Batch(id="ok", gtin=GTIN, codes=[CODE]))
    with open(os.path.join(issue.folder(), "bad.json"), "w", encoding="utf-8") as handle:
        handle.write("{не json")

    assert [batch.id for batch in issue.saved()] == ["ok"]


def test_удалённый_блок_уходит_из_списка_но_файл_цел():
    batch = issue.Batch(id="b", gtin=GTIN, codes=[CODE] * 3)
    issue.save(batch)

    moved = issue.delete(batch)

    assert issue.saved() == []
    assert issue.load("b") is None
    # Оплаченные коды не стёрты: их можно вернуть, переложив файл обратно.
    assert os.path.dirname(moved) == os.path.join(issue.folder(), issue.TRASH)
    with open(moved, encoding="utf-8") as handle:
        assert json.load(handle)["codes"] == [CODE] * 3


def test_подпапка_удалённых_не_попадает_в_список():
    issue.save(issue.Batch(id="a", gtin=GTIN, codes=[CODE]))
    issue.save(issue.Batch(id="b", gtin=GTIN, codes=[CODE]))
    issue.delete(issue.load("b"))

    assert [batch.id for batch in issue.saved()] == ["a"]


def test_сколько_кодов_заказа_сохранено_на_компьютере():
    issue.save(issue.Batch(id="a", order_id="o1", gtin=GTIN, codes=[CODE] * 4))
    issue.save(issue.Batch(id="b", order_id="o1", gtin="2", codes=[CODE] * 2))
    issue.save(issue.Batch(id="c", order_id="o2", gtin=GTIN, codes=[CODE] * 7))

    assert issue.stored_for("o1") == 6
    assert issue.stored_for("o1", GTIN) == 4
    assert issue.stored_for("нет") == 0


# --- ввод в оборот ----------------------------------------------------------------

def _document(**changes):
    values = {"inn": "250101802801", "tnved": "6115950000", "codes": (CODE,)}
    values.update(changes)
    return introduce.Document(**values)


def test_в_документ_идёт_код_идентификации_без_криптохвоста():
    text = introduce.build(_document())

    assert f"<![CDATA[01{GTIN}21Xy7Q2aB9pLm4]]>" in text
    assert "91EE11" not in text and GS not in text


def test_состав_документа_как_в_шаблоне():
    text = introduce.build(_document(codes=(CODE, CODE.replace("Xy7Q", "Zz7Q"))))

    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n'
                           '<vvod_ostatky version="3">')
    assert "<trade_participant_inn>250101802801</trade_participant_inn>" in text
    assert text.count("<product>") == 2
    assert text.count("<tnved_code>6115950000</tnved_code>") == 2
    assert text.rstrip().endswith("</vvod_ostatky>")


@pytest.mark.parametrize("changes, message", [
    ({"inn": "123"}, "ИНН"),
    ({"tnved": ""}, "ТН ВЭД"),
    ({"codes": ()}, "нет ни одного кода"),
])
def test_неполный_документ_не_собирается(changes, message):
    with pytest.raises(introduce.IntroduceProblem, match=message):
        introduce.build(_document(**changes))


def test_неразобранный_код_не_попадает_в_документ_молча():
    with pytest.raises(introduce.IntroduceProblem, match="не разбирается"):
        introduce.build(_document(codes=("мусор",)))


def test_конец_cdata_внутри_кода_не_рвёт_разметку():
    assert "]]>" not in introduce._cdata("a]]>b").replace("]]]]><![CDATA[>", "")


def test_длинная_партия_делится_на_документы():
    parts = introduce.split(["x"] * (introduce.MAX_PRODUCTS * 2 + 1))

    assert [len(part) for part in parts] == [introduce.MAX_PRODUCTS] * 2 + [1]


# --- настройки этикетки ----------------------------------------------------------

def test_настройки_печати_переживают_перезапуск():
    spec = labels.LabelSpec(width=58, height=40, dm_size=20, offset_x=1.5,
                            show_name=False)
    labels.save(labels.PrintSettings(printer="PT10", label=spec))

    loaded = labels.load()

    assert loaded.printer == "PT10"
    assert (loaded.spec().width, loaded.spec().height) == (58, 40)
    assert loaded.spec().offset_x == 1.5 and loaded.spec().show_name is False


def test_испорченный_файл_настроек_это_значения_по_умолчанию():
    os.makedirs(os.path.dirname(labels.path()), exist_ok=True)
    with open(labels.path(), "w", encoding="utf-8") as handle:
        handle.write("[1,2")

    assert labels.load().spec().width == 43


def test_символ_шире_этикетки_называется_проблемой():
    assert labels.LabelSpec(width=43, dm_left=30, dm_size=15).problems
    assert not labels.LabelSpec().problems


# --- отправка ввода в оборот из программы -----------------------------------------------

import base64


@pytest.fixture
def online(served, monkeypatch):
    """Вход выполнен, подпись подменена записью о том, что подписывали."""
    signed: list = []
    monkeypatch.setattr(introduce.session, "authorized", lambda call: call("TOKEN"))
    monkeypatch.setattr(
        introduce.crypto, "sign",
        lambda data, thumbprint, detached=False: signed.append((data, detached)) or "ПОДПИСЬ")
    calls, answers = served
    return calls, answers, signed


def test_документ_уходит_единым_методом_в_формате_принтмарки(online):
    calls, answers, signed = online
    answers.append("5ee0cb4f-b2c4-4f3e-a1a1-0123456789ab")

    doc_id = introduce.submit(_document(), "ААББ", "lp", Contour.SANDBOX)

    assert doc_id == "5ee0cb4f-b2c4-4f3e-a1a1-0123456789ab"
    request = calls[0]
    assert "/api/v3/true-api/lk/documents/create" in request.full_url
    assert "pg=lp" in request.full_url
    assert request.get_header("Authorization") == "Bearer TOKEN"
    body = json.loads(request.data)
    assert set(body) == {"document_format", "product_document", "type", "signature"}
    assert body["document_format"] == "XML"
    assert body["type"] == "LP_INTRODUCE_OST_XML"
    assert body["signature"] == "ПОДПИСЬ"


def test_подписывается_тот_же_текст_что_уходит_в_запрос(online):
    """Иначе ответ — «подпись невалидна», и догадаться, почему, нельзя."""
    calls, answers, signed = online
    answers.append("5ee0cb4f-b2c4-4f3e-a1a1-0123456789ab")

    introduce.submit(_document(), "ААББ", "lp")

    sent_xml = base64.b64decode(json.loads(calls[0].data)["product_document"]).decode("utf-8")
    assert signed == [(sent_xml, True)]      # и подпись откреплённая
    assert sent_xml == introduce.build(_document())


def test_отправка_не_повторяется_при_обрыве_связи(online):
    calls, answers, _ = online
    answers.append(transport.Offline("сети нет"))

    with pytest.raises(introduce.IntroduceUnknown, match="личном кабинете"):
        introduce.submit(_document(), "ААББ", "lp")

    assert len(calls) == 1


def test_ответ_без_номера_документа_не_выдаётся_за_успех(online):
    _, answers, _ = online
    answers.append({"что-то": "другое"})

    with pytest.raises(introduce.IntroduceUnknown):
        introduce.submit(_document(), "ААББ", "lp")


def test_номер_документа_принимается_и_объектом(online):
    _, answers, _ = online
    answers.append({"docId": "abc-123-def-456"})

    assert introduce.submit(_document(), "ААББ", "lp") == "abc-123-def-456"


def test_группа_без_типа_документа_не_отправляется_и_не_подписывается(online):
    calls, _, signed = online

    with pytest.raises(introduce.IntroduceProblem, match="не подключена"):
        introduce.submit(_document(), "ААББ", "perfumery")

    assert calls == [] and signed == []


def test_без_сертификата_документ_не_отправляется(online):
    calls, _, _ = online

    with pytest.raises(introduce.IntroduceProblem, match="сертификат"):
        introduce.submit(_document(), "", "lp")
    assert calls == []


def _list(status, errors=None, number="doc-1"):
    row = {"number": number, "status": status}
    if errors:
        row["errors"] = errors
    return {"results": [row]}


def test_статус_читается_из_списка_документов(online):
    calls, answers, _ = online
    answers.append(_list("CHECKED_OK"))

    result = introduce.status("doc-1", "lp", Contour.SANDBOX)

    assert result.done and not result.failed
    assert "number=doc-1" in calls[0].full_url and "pg=lp" in calls[0].full_url
    assert "/doc/list" in calls[0].full_url


def test_отказ_приносит_тексты_ошибок(online):
    _, answers, _ = online
    answers.append(_list("CHECKED_NOT_OK", ["Код уже в обороте", {"text": "Неверный ТН ВЭД"}]))

    result = introduce.status("doc-1", "lp")

    assert result.failed
    assert result.errors == ("Код уже в обороте", "Неверный ТН ВЭД")
    assert "Код уже в обороте" in result.text


def test_незнакомый_статус_не_считается_ни_успехом_ни_ожиданием(online):
    _, answers, _ = online
    # Второй ответ — карточка документа, к которой идут за причиной.
    answers.extend([_list("НЕЧТО_НОВОЕ"), {}])

    result = introduce.status("doc-1", "lp")

    assert not result.done and not result.failed and not result.pending
    assert result.title == "НЕЧТО_НОВОЕ"


def test_пустой_ответ_это_нет_данных():
    assert introduce.DocStatus().title == "нет данных"
    assert not introduce.DocStatus().done


def test_отправка_ждёт_итога_и_возвращает_успех(online):
    calls, answers, _ = online
    pauses: list = []
    answers.extend(["doc-1", _list("IN_PROGRESS"), _list("IN_PROGRESS"),
                    _list("CHECKED_OK")])
    created: list = []

    sent = introduce.send(_document(), "ААББ", "lp", on_created=created.append,
                          interval=3, sleep=pauses.append)

    assert sent.doc_id == "doc-1" and sent.status.done
    assert created == ["doc-1"]
    assert pauses == [3, 3]
    assert len(calls) == 4


def test_отказ_возвращается_сразу_без_ожидания(online):
    _, answers, _ = online
    pauses: list = []
    answers.extend(["doc-1", _list("CHECKED_NOT_OK", ["не принят"])])

    sent = introduce.send(_document(), "ААББ", "lp", sleep=pauses.append)

    assert sent.status.failed and pauses == []


def test_без_итога_за_отведённое_время_документ_остаётся_созданным(online):
    _, answers, _ = online
    pauses: list = []
    answers.append("doc-1")
    answers.extend([_list("IN_PROGRESS")] * 30)

    sent = introduce.send(_document(), "ААББ", "lp", wait=6, interval=3,
                          sleep=pauses.append)

    assert sent.doc_id == "doc-1"
    assert sent.status.pending and not sent.status.done
    assert pauses == [3, 3]


def test_сбой_опроса_не_теряет_номер_созданного_документа(online):
    _, answers, _ = online
    created: list = []
    answers.extend(["doc-1", transport.MarkingError("сервис недоступен", 400)])

    sent = introduce.send(_document(), "ААББ", "lp", on_created=created.append,
                          sleep=lambda _: None)

    assert created == ["doc-1"] and sent.doc_id == "doc-1"
    assert "статус не получен" in sent.status.errors[0]


class _Response:
    """Ответ сервера для подмены urlopen."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_голый_идентификатор_в_ответе_принимается_транспортом(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *args, **kwargs: _Response(b"5ee0cb4f-b2c4-4f3e-a1a1"))

    assert transport._send(urllib.request.Request("https://example.invalid/x")) == \
        "5ee0cb4f-b2c4-4f3e-a1a1"


def test_страница_ошибки_вместо_json_по_прежнему_ошибка(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: _Response(
        "<html>Ошибка шлюза</html>".encode("utf-8")))

    with pytest.raises(transport.MarkingError, match="не JSON"):
        transport._send(urllib.request.Request("https://example.invalid/x"))


def test_отправленный_документ_запоминается_в_блоке():
    batch = issue.Batch(id="b9", gtin=GTIN, codes=[CODE] * 6, printed=6)
    issue.save(batch)

    issue.mark_sent(batch, "doc-1", 4, "IN_PROGRESS")
    again = issue.load("b9")

    assert (again.doc_id, again.introduced_count, again.doc_status) == (
        "doc-1", 4, "IN_PROGRESS")
    assert again.introduced
    issue.mark_status(again, "CHECKED_OK")
    assert issue.load("b9").doc_status == "CHECKED_OK"
    # Покрытое число назад не откатывается.
    issue.mark_sent(again, "doc-2", 2)
    assert issue.load("b9").introduced_count == 4


# --- опрос статуса: версии API и запасной способ ---------------------------------------

def test_список_документов_спрашивается_в_четвёртой_версии(online):
    """В третьей `/doc/list` отвечает «Метод с указанным URL не найден»."""
    calls, answers, _ = online
    answers.append(_list("CHECKED_OK"))

    introduce.status("doc-1", "lp", Contour.SANDBOX)

    assert "/api/v4/true-api/doc/list" in calls[0].full_url


def test_не_найден_список_документов_пробуется_карточка_документа(online):
    calls, answers, _ = online
    answers.extend([
        transport.MarkingError("Метод с указанным URL не найден", 404),
        {"number": "doc-1", "status": "CHECKED_OK"},
    ])

    result = introduce.status("doc-1", "lp")

    assert result.done
    assert "/api/v3/true-api/doc/doc-1/info" in calls[1].full_url


def test_иная_ошибка_запасной_способ_не_запускает(online):
    calls, answers, _ = online
    answers.append(transport.MarkingError("Доступ запрещён", 403))

    with pytest.raises(transport.MarkingError, match="Доступ запрещён"):
        introduce.status("doc-1", "lp")

    assert len(calls) == 1


def test_если_не_найдено_везде_ошибка_последнего_способа(online):
    _, answers, _ = online
    answers.extend([transport.MarkingError("Метод с указанным URL не найден", 404),
                    transport.MarkingError("Документ не найден", 404)])

    with pytest.raises(transport.MarkingError, match="Документ не найден"):
        introduce.status("doc-1", "lp")


# --- PARSE_ERROR и причина отказа -------------------------------------------------------

def test_parse_error_это_отказ_а_не_ожидание():
    status = introduce.DocStatus("PARSE_ERROR")

    assert status.failed and not status.pending and not status.done
    assert "формата" in status.title


@pytest.mark.parametrize("code", ["PARSING_ERROR", "PROCESSING_ERROR", "SOME_NEW_ERROR",
                                  "CHECKED_NOT_OK", "REJECTED"])
def test_ошибочные_статусы_считаются_отказом(code):
    assert introduce.DocStatus(code).failed


@pytest.mark.parametrize("code", ["IN_PROGRESS", "WAIT_FOR_CONTINUATION",
                                  "WAIT_ACCEPTANCE"])
def test_ожидающие_статусы_итогом_не_считаются(code):
    status = introduce.DocStatus(code)

    assert status.pending and not status.failed and not status.done


def test_незнакомый_статус_не_отказ_и_не_успех():
    status = introduce.DocStatus("НЕЧТО_НОВОЕ")

    assert not (status.failed or status.done or status.pending)


def test_отправка_не_ждёт_после_parse_error(online):
    _, answers, _ = online
    pauses: list = []
    answers.extend(["doc-1", _list("PARSE_ERROR")])
    # Причину список не принёс — она запрашивается у карточки документа.
    answers.append({"status": "PARSE_ERROR",
                    "errors": ["cvc-complex-type.2.4.a: ожидался элемент tnved_code"]})

    sent = introduce.send(_document(), "ААББ", "lp", sleep=pauses.append)

    assert sent.status.failed and pauses == []
    assert "tnved_code" in sent.status.text


def test_причина_из_списка_документов_карточку_не_запрашивает(online):
    calls, answers, _ = online
    answers.append(_list("PARSE_ERROR", ["Элемент ki пуст"]))

    result = introduce.status("doc-1", "lp")

    assert result.errors == ("Элемент ki пуст",)
    assert len(calls) == 1


def test_ответ_об_отказе_записывается_в_журнал(online, profile):
    _, answers, _ = online
    answers.append(_list("PARSE_ERROR", ["Элемент ki пуст"]))

    introduce.status("doc-1", "lp")

    log = (profile / "RetailCore" / "marking.log").read_text(encoding="utf-8")
    assert "Статус документа doc-1" in log and "Элемент ki пуст" in log


def test_тнвэд_должен_быть_ровно_десять_цифр():
    """Четыре знака — группа товаров, а не код; в файле система ждёт полный."""
    assert introduce.Document("250101802801", "6115", (CODE,)).problems
    assert introduce.Document("250101802801", "61159500001", (CODE,)).problems
    assert not introduce.Document("250101802801", "6115950000", (CODE,)).problems


def test_отказ_освобождает_коды_для_повторной_отправки():
    batch = issue.Batch(id="b10", gtin=GTIN, codes=[CODE] * 3, printed=3)
    issue.save(batch)
    issue.mark_sent(batch, "doc-1", 3, "IN_PROGRESS")

    issue.mark_status(batch, "PARSE_ERROR", failed=True)

    again = issue.load("b10")
    assert again.introduced_count == 0 and again.doc_status == "PARSE_ERROR"
    # Номер неудачного документа остаётся — по нему видна история.
    assert again.doc_id == "doc-1"


def test_успех_не_освобождает_коды():
    batch = issue.Batch(id="b11", gtin=GTIN, codes=[CODE] * 3, printed=3)
    issue.save(batch)
    issue.mark_sent(batch, "doc-1", 3)

    issue.mark_status(batch, "CHECKED_OK", failed=False)

    assert issue.load("b11").introduced_count == 3


def test_корень_документа_без_устаревших_атрибутов():
    """Версия 2 с action_id система отвергает: «не соответствует XSD-схеме»."""
    text = introduce.build(_document())

    assert '<vvod_ostatky version="3">' in text
    assert "action_id" not in text


# --- перемаркировка (LK_REMARK) ---------------------------------------------------------

OLD = f"01{GTIN}21OldSerial{GS}91EE10{GS}92c3RhcmFnZQ=="


def _remark(**changes):
    values = dict(inn="250101802801", tnved="6109909000", cause="KM_SPOILED",
                  date="2026-10-05", codes=(CODE,))
    values.update(changes)
    return introduce.Remark(**values)


def test_перемаркировка_без_старого_кода_для_испорчено_или_утеряно():
    """Так в ПРИНТМАРКИ: для KM_SPOILED предыдущий КИ указывать не обязательно."""
    body = json.loads(_remark().render(".json"))

    assert body == {
        "participant_inn": "250101802801",
        "remarking_date": "2026-10-05",
        "remarking_cause": "KM_SPOILED",
        "products": [{"new_uin": f"01{GTIN}21Xy7Q2aB9pLm4", "tnved_10": "6109909000"}],
    }


def test_в_документ_идёт_ки_без_криптохвоста_и_старый_в_том_же_порядке():
    other = CODE.replace("Xy7Q", "Zz7Q")
    body = json.loads(_remark(codes=(CODE, other),
                              previous=(OLD, OLD.replace("OldSerial", "Older"))).render())

    assert body["products"][0]["last_uin"] == f"01{GTIN}21OldSerial"
    assert body["products"][1]["last_uin"] == f"01{GTIN}21Older"
    assert body["products"][1]["new_uin"] == f"01{GTIN}21Zz7Q2aB9pLm4"
    assert "91EE" not in json.dumps(body)


def test_сведения_о_товаре_и_первичный_документ_попадают_в_каждую_позицию():
    body = json.loads(_remark(country="156", color="чёрный", size="M",
                              doc_type="OTHER", doc_number="12",
                              doc_date="2026-10-01", doc_name="Акт").render())

    item = body["products"][0]
    assert item["production_country"] == "156" and item["color"] == "чёрный"
    assert item["product_size"] == "M"
    assert item["primary_document_type"] == "OTHER"
    assert item["primary_document_custom_name"] == "Акт"
    assert item["primary_document_number"] == "12"
    assert item["primary_document_date"] == "2026-10-01"


def test_наименование_первичного_документа_только_для_иного():
    body = json.loads(_remark(doc_type="RECEIPT", doc_number="5",
                              doc_date="2026-10-01", doc_name="лишнее").render())

    assert "primary_document_custom_name" not in body["products"][0]


def test_xml_перемаркировки_повторяет_шаблон_принтмарки():
    text = _remark(previous=(OLD,), color="чёрный").render(".xml")

    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<remark version="7">')
    assert "<remark_cause>KM_SPOILED</remark_cause>" in text
    assert f"<last_ki><![CDATA[01{GTIN}21OldSerial]]></last_ki>" in text
    assert f"<new_ki><![CDATA[01{GTIN}21Xy7Q2aB9pLm4]]></new_ki>" in text
    assert "<tnved_code_10>6109909000</tnved_code_10>" in text
    assert "<color>чёрный</color>" in text
    assert text.rstrip().endswith("</remark>")


@pytest.mark.parametrize("changes, message", [
    ({"inn": "1"}, "ИНН"),
    ({"tnved": "6109"}, "ТН ВЭД"),
    ({"cause": "ЧТО_ТО"}, "причина"),
    ({"date": "05.10.2026"}, "ГГГГ-ММ-ДД"),
    ({"codes": ()}, "нет ни одного нового кода"),
    ({"previous": (OLD, OLD)}, "поровну"),
    ({"cause": "DESCRIPTION_ERRORS"}, "нужен предыдущий КИ"),
    ({"country": "Китай"}, "ОКСМ"),
    ({"doc_type": "RECEIPT"}, "первичный документ"),
    ({"doc_type": "OTHER", "doc_number": "1", "doc_date": "2026-10-01"}, "наименование"),
])
def test_неполная_перемаркировка_не_собирается(changes, message):
    remark = _remark(**changes)

    assert any(message in problem for problem in remark.problems)
    with pytest.raises(introduce.IntroduceProblem):
        remark.render()


def test_для_ошибок_описания_старый_код_обязателен_и_с_ним_документ_собирается():
    assert _remark(cause="DESCRIPTION_ERRORS", previous=(OLD,)).problems == []


def test_перемаркировка_уходит_как_lk_remark_в_формате_manual(online):
    calls, answers, signed = online
    answers.append("5ee0cb4f-b2c4-4f3e-a1a1-0123456789ab")
    remark = _remark()

    introduce.submit(remark, "ААББ", "lp", Contour.SANDBOX)

    body = json.loads(calls[0].data)
    assert body["type"] == "LK_REMARK" and body["document_format"] == "MANUAL"
    assert "pg=lp" in calls[0].full_url
    sent = base64.b64decode(body["product_document"]).decode("utf-8")
    assert json.loads(sent)["remarking_cause"] == "KM_SPOILED"
    # Подписывается ровно тот текст, что уходит, и подпись откреплённая.
    assert signed == [(sent, True)]


def test_перемаркировка_допустима_для_любой_группы_а_остатки_нет(online):
    calls, answers, _ = online
    answers.append("5ee0cb4f-b2c4-4f3e-a1a1-0123456789ab")

    introduce.submit(_remark(), "ААББ", "perfumery")

    assert json.loads(calls[0].data)["type"] == "LK_REMARK"
    with pytest.raises(introduce.IntroduceProblem, match="не подключена"):
        introduce.submit(_document(), "ААББ", "perfumery")


def test_перемаркировка_без_группы_не_отправляется(online):
    calls, _, _ = online

    with pytest.raises(introduce.IntroduceProblem, match="товарная группа"):
        introduce.submit(_remark(), "ААББ", "")
    assert calls == []


def test_имя_файла_учитывает_вид_документа_и_расширение():
    name = introduce.file_name(GTIN, extension=".json", title="Перемаркировка")

    assert name.startswith(f"Перемаркировка {GTIN} ") and name.endswith(".json")


def test_сведения_о_товаре_блока_переживают_перезапуск():
    batch = issue.Batch(id="b12", gtin=GTIN, codes=[CODE], country="156",
                        color="чёрный", size="M")
    issue.save(batch)

    again = issue.load("b12")

    assert (again.country, again.color, again.size) == ("156", "чёрный", "M")


def test_вид_документа_и_причина_запоминаются_на_рабочем_месте():
    settings = labels.PrintSettings(introduce_kind="ostatky",
                                    remark_cause="RETAIL_RETURN")
    labels.save(settings)

    loaded = labels.load()

    assert loaded.introduce_kind == "ostatky" and loaded.remark_cause == "RETAIL_RETURN"
    assert labels.PrintSettings().introduce_kind == "remark"
    assert labels.PrintSettings().remark_cause == "KM_SPOILED"
