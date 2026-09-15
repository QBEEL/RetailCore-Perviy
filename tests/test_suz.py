"""СУЗ: адрес, реквизиты соединения, проверка связи и хранение токена.

Сеть здесь не трогается: подменяется отправка запроса, а проверяется то, что
до неё дошло — какой адрес собран и каким заголовком предъявлен токен. Файл
реквизитов уводится в `tmp_path` через `APPDATA`: тесты не должны трогать
настоящий профиль пользователя, в котором лежит рабочий токен.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta

import pytest

from app.core.marking import suz, transport
from app.core.marking.models import Contour

OMS = "b1a2c3d4-0000-1111-2222-333344445555"
CONNECTION = "9f8e7d6c-5555-4444-3333-222211110000"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.token-of-the-device.signature"

FULL = suz.Credentials(oms_id=OMS, connection_id=CONNECTION, token=TOKEN)


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    """Профиль пользователя на время теста — свой, пустой и одноразовый."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    yield tmp_path
    # Адрес правится глобально: тест, вписавший свой, не должен доставаться
    # следующему.
    suz.apply(suz.Credentials(), Contour.SANDBOX)
    suz.apply(suz.Credentials(), Contour.PRODUCTION)


@pytest.fixture
def sent(monkeypatch):
    """Запросы, дошедшие до отправки, вместо обращения к сети."""
    calls: list = []
    answer: dict = {"body": {"omsId": OMS}, "error": None}

    def send(prepared, tolerate=()):
        calls.append(prepared)
        if answer["error"] is not None:
            raise answer["error"]
        return answer["body"]

    monkeypatch.setattr(transport, "_send", send)
    transport.limiter.reset()
    return calls, answer


# --- адрес ------------------------------------------------------------------------

def test_суз_отдельная_система_со_своим_адресом():
    """СУЗ — не раздел True API: и хост, и раздел у неё свои."""
    live = transport.url("suz", suz.PING_PATH, Contour.PRODUCTION)
    test = transport.url("suz", suz.PING_PATH, Contour.SANDBOX)

    assert live != transport.url("trueapi", suz.PING_PATH, Contour.PRODUCTION)
    assert live.endswith("/api/v3/ping")
    assert "sandbox" in test


def test_свой_адрес_подменяет_встроенный_а_пустой_возвращает_его():
    """Локальная станция стоит в сети предприятия, и адрес у неё свой —
    знать его заранее приложение не может."""
    built_in = transport.url("suz", suz.PING_PATH, Contour.SANDBOX)

    suz.apply(suz.Credentials(host="https://suz.example.ru/"), Contour.SANDBOX)
    assert transport.url("suz", suz.PING_PATH, Contour.SANDBOX) == (
        "https://suz.example.ru/api/v3/ping")

    suz.apply(suz.Credentials(), Contour.SANDBOX)
    assert transport.url("suz", suz.PING_PATH, Contour.SANDBOX) == built_in


# --- проверка связи ----------------------------------------------------------------

def test_проверка_предъявляет_токен_своим_заголовком(sent):
    """СУЗ ждёт токен в `clientToken`, а не в `Authorization: Bearer`."""
    calls, _ = sent

    suz.check(FULL, Contour.SANDBOX)

    prepared = calls[0]
    assert prepared.get_header("Clienttoken") == TOKEN
    assert prepared.get_header("Authorization") is None
    assert f"omsId={OMS}" in prepared.full_url


def test_проверка_ничего_не_отправляет_и_не_заказывает(sent):
    """`ping` — чтение: тела у запроса нет, кодов он не тратит."""
    calls, _ = sent

    suz.check(FULL, Contour.PRODUCTION)

    assert calls[0].data is None
    assert calls[0].get_method() == "GET"


def test_незаполненные_реквизиты_названы_до_обращения_к_сети(sent):
    calls, _ = sent

    with pytest.raises(transport.MarkingError) as failure:
        suz.check(suz.Credentials(oms_id=OMS), Contour.SANDBOX)

    assert "токен" in str(failure.value)
    assert calls == []


def test_без_идентификатора_соединения_работать_можно(sent):
    """Проверено обращением: в запросы он не уходит вовсе.

    Требовать реквизит, которого не спрашивает ни один метод, значит остановить
    человека на пустом месте.
    """
    calls, _ = sent
    without = suz.Credentials(oms_id=OMS, token=TOKEN)

    assert without.filled
    suz.check(without, Contour.SANDBOX)

    assert "connectionId" not in calls[0].full_url


def test_непринятые_реквизиты_объяснены_а_не_показаны_кодом(sent):
    """Отказ СУЗ приходит четырёхсотым, а не 401.

    Проверено обращением: `ping` без токена отвечает 400 и «Проверка учетных
    данных УОТ не пройдена». Ждать здесь 401, как от True API, значит показать
    общий текст про сертификат — которого в СУЗ нет вовсе.
    """
    _, answer = sent
    answer["error"] = transport.MarkingError(
        "Проверка учетных данных УОТ не пройдена (код 1110)", 400)

    with pytest.raises(transport.MarkingError) as failure:
        suz.check(FULL, Contour.SANDBOX)

    text = str(failure.value)
    assert "сертификат" not in text.lower()
    assert "ОМС ID" in text
    # Ответ системы доходит до человека дословно, вместе с кодом ошибки: по
    # нему в поддержке и разбираются.
    assert "Проверка учетных данных УОТ не пройдена (код 1110)" in text


def test_ошибка_суз_читается_из_своего_списка():
    """У СУЗ текст ошибки лежит списком под `globalErrors`, а не строкой."""
    body = (b'{"globalErrors":[{"errorCode":1110,"error":'
            b'"\\u041d\\u0435\\u0442"}],"success":false}')

    error = transport._failure(400, body)

    assert "Нет" in str(error)
    assert "1110" in str(error)


def test_недоступность_суз_не_выдаётся_за_отказ_в_реквизитах(sent, monkeypatch):
    """Оборванная сеть — не повод сказать «токен не принят»."""
    _, answer = sent
    answer["error"] = transport.Offline("Система маркировки недоступна")
    # Недоступность транспорт повторяет, и ждать здесь настоящие двенадцать
    # секунд незачем — проверяется не выдержка, а разбор отказа.
    monkeypatch.setattr(transport, "RETRY_DELAYS", (0.0, 0.0, 0.0))

    with pytest.raises(transport.Offline):
        suz.check(FULL, Contour.SANDBOX)


def test_русская_буква_в_токене_объяснена_а_не_роняет_запрос():
    """Заголовки уходят latin-1: кириллица в токене роняет `urllib` до отправки.

    Само исключение говорит только про «position 0-1» — человеку из него
    неясно ничего, а вставить токен в русской раскладке проще простого.
    """
    prepared = urllib.request.Request(
        "https://suz.example.invalid/api/v3/ping",
        headers={suz.TOKEN_HEADER: "токен"})

    with pytest.raises(transport.MarkingError, match="русские буквы"):
        transport._send(prepared)


def test_ответ_про_чужой_омс_считается_ошибкой(sent):
    """Токен той станции, но ОМС вписан чужой — заказ ушёл бы не туда."""
    _, answer = sent
    answer["body"] = {"omsId": "00000000-9999-8888-7777-666655554444"}

    with pytest.raises(transport.MarkingError, match="другой ОМС"):
        suz.check(FULL, Contour.SANDBOX)


def test_удачная_проверка_называет_контур_и_омс(sent):
    told = suz.check(FULL, Contour.PRODUCTION)

    assert OMS in told
    assert Contour.PRODUCTION.title in told


# --- хранение ---------------------------------------------------------------------

def test_реквизиты_переживают_перезапуск():
    suz.save(FULL, Contour.SANDBOX)

    assert suz.load(Contour.SANDBOX) == FULL
    assert suz.known(Contour.SANDBOX)


def test_у_каждого_контура_свои_реквизиты():
    """Устройство в песочнице и устройство в бою — разные, и путать их нельзя."""
    suz.save(FULL, Contour.SANDBOX)
    suz.save(suz.Credentials(oms_id="боевой", connection_id="соединение",
                             token="токен"), Contour.PRODUCTION)

    assert suz.load(Contour.SANDBOX).oms_id == OMS
    assert suz.load(Contour.PRODUCTION).oms_id == "боевой"

    suz.forget(Contour.PRODUCTION)

    assert suz.load(Contour.SANDBOX) == FULL
    assert not suz.known(Contour.PRODUCTION)


def test_пробелы_из_буфера_обмена_не_доезжают_до_заголовка():
    """Хвостовой перенос строки в заголовке — отказ, объяснить который нечем."""
    saved = suz.save(suz.Credentials(oms_id=f" {OMS}\n", connection_id=f"{CONNECTION} ",
                                     token=f"\t{TOKEN} ", host="https://suz.example.ru/"),
                     Contour.SANDBOX)

    assert saved == suz.Credentials(oms_id=OMS, connection_id=CONNECTION,
                                    token=TOKEN, host="https://suz.example.ru")


def test_токен_лежит_отдельно_от_общих_настроек():
    """`settings.json` копируют и пересылают — токену там не место."""
    suz.save(FULL, Contour.SANDBOX)

    assert suz.path().endswith(suz.FILE_NAME)
    assert "settings" not in suz.FILE_NAME
    with open(suz.path(), encoding="utf-8") as handle:
        assert json.load(handle)["sandbox"]["token"] == TOKEN


def test_испорченный_файл_читается_как_пустой():
    os.makedirs(os.path.dirname(suz.path()), exist_ok=True)
    with open(suz.path(), "w", encoding="utf-8") as handle:
        handle.write("{это не json")

    assert suz.load(Contour.SANDBOX) == suz.Credentials()


def test_сохранённый_адрес_применяется_при_запуске():
    """Первый же запрос обязан уйти по вписанному адресу, а не по встроенному."""
    suz.save(suz.Credentials(oms_id=OMS, connection_id=CONNECTION, token=TOKEN,
                             host="https://suz.example.ru"), Contour.SANDBOX)
    suz.apply(suz.Credentials(), Contour.SANDBOX)   # как будто приложение перезапустили

    suz.restore()

    assert transport.url("suz", suz.PING_PATH, Contour.SANDBOX) == (
        "https://suz.example.ru/api/v3/ping")


# --- показ ------------------------------------------------------------------------

def test_токен_показывается_огрызком():
    """По началу и концу видно, тот ли токен вписан; воспользоваться — нельзя."""
    shown = suz.mask(TOKEN)

    assert TOKEN not in shown
    assert shown.startswith(TOKEN[:4])
    assert shown.endswith(TOKEN[-4:])


def test_короткий_токен_скрывается_целиком():
    assert suz.mask("короткий") == "•" * len("короткий")
    assert suz.mask("") == ""


def test_чего_не_хватает_названо_по_русски():
    assert suz.Credentials(token=TOKEN).missing == ("ОМС ID",)
    assert suz.Credentials(oms_id=OMS).missing == ("токен",)


# --- вход по сертификату -------------------------------------------------------------

def test_токен_добывается_подписью_а_не_переносом_из_чужой_программы(sent, monkeypatch):
    """У СУЗ свой вход по УКЭП, устроенный как в True API.

    Проверено обращением: `/auth/key` отвечает строкой для подписи, а
    `simpleSignIn` с непригодной подписью жалуется именно на подпись.
    """
    calls, answer = sent
    answers = [{"uuid": "u-1", "data": "подписать-это"}, {"token": "выданный-токен"}]
    answer["body"] = answers

    def send(prepared, tolerate=()):
        calls.append(prepared)
        return answers[len(calls) - 1]

    monkeypatch.setattr(transport, "_send", send)
    monkeypatch.setattr(suz.crypto, "find", lambda thumbprint: object())
    monkeypatch.setattr(suz.crypto, "sign", lambda data, thumbprint: f"подпись({data})")

    token = suz.sign_in("ААББ", contour=Contour.SANDBOX)

    assert token == "выданный-токен"
    assert calls[0].full_url.endswith("/api/v3/auth/key")
    assert json.loads(calls[1].data) == {"uuid": "u-1", "data": "подпись(подписать-это)"}


def test_вход_без_носителя_ключа_говорит_об_этом_прямо(monkeypatch):
    monkeypatch.setattr(suz.crypto, "find", lambda thumbprint: None)

    with pytest.raises(suz.crypto.SigningFailed, match="носитель"):
        suz.sign_in("ААББ", contour=Contour.SANDBOX)


def test_полученный_токен_запоминает_когда_он_выдан():
    """Срок жизни токена СУЗ не объявлен — дата выдачи его и заменяет."""
    fresh = suz.renewed(FULL, "новый-токен")

    assert fresh.token == "новый-токен"
    assert fresh.oms_id == OMS
    assert fresh.issued_at

    suz.save(fresh, Contour.SANDBOX)

    assert suz.load(Contour.SANDBOX).issued_at == fresh.issued_at


# --- срок жизни токена ----------------------------------------------------------------

def test_срок_токена_считается_от_выдачи():
    """Токен СУЗ живёт десять часов, а в ответе срок не приходит."""
    fresh = suz.renewed(FULL, TOKEN)

    assert not fresh.expiring
    assert fresh.expires_in is not None
    assert suz.LIFETIME - fresh.expires_in < timedelta(minutes=1)


def test_токен_без_отметки_времени_считается_протухшим():
    """Перенесённый из другой программы мог быть выдан вчера."""
    assert FULL.expiring
    assert FULL.expires_in is None


def test_выданный_вчера_токен_пора_обновлять():
    yesterday = (datetime.now() - timedelta(hours=11)).isoformat(timespec="minutes")

    assert suz.Credentials(oms_id=OMS, token=TOKEN, issued_at=yesterday).expiring


def test_отказ_по_учётным_данным_отличён_от_прочих():
    """По нему и понятно, что лечится он обновлением токена, а не правкой полей."""
    assert suz.stale(transport.MarkingError(
        "Проверка учетных данных УОТ не пройдена (код 1110)", 400))
    assert not suz.stale(transport.MarkingError("Метод не поддерживается", 405))
    assert not suz.stale(transport.Offline("сети нет"))


def test_идентификатор_соединения_уходит_в_запрос_токена(sent, monkeypatch):
    """Токен выдаётся под конкретное устройство — без соединения запрос не о чем."""
    calls, _ = sent
    answers = [{"uuid": "u-1", "data": "подписать"}, {"token": "выданный"}]

    def send(prepared, tolerate=()):
        calls.append(prepared)
        return answers[len(calls) - 1]

    monkeypatch.setattr(transport, "_send", send)
    monkeypatch.setattr(suz.crypto, "find", lambda thumbprint: object())
    monkeypatch.setattr(suz.crypto, "sign", lambda data, thumbprint: "подпись")

    suz.sign_in("ААББ", contour=Contour.SANDBOX, connection_id=CONNECTION)

    body = json.loads(calls[1].data)
    assert body["omsConnection"] == CONNECTION
    # И параметром тоже: какое из двух мест верное, из чужой библиотеки не
    # видно, а каждая попытка стоит человеку пароля к контейнеру.
    assert f"omsConnection={CONNECTION}" in calls[1].full_url


# --- обновление протухшего токена -------------------------------------------------

def test_протухший_токен_обновляется_сам_а_не_походом_в_другую_программу(monkeypatch):
    """Токен живёт десять часов; продлевать его руками — не работа человека."""
    from app.core.marking import service

    tries: list = []

    def call(credentials):
        tries.append(credentials.token)
        if len(tries) == 1:
            raise transport.MarkingError(
                "Проверка учетных данных УОТ не пройдена (код 1110)", 400)
        return "получилось"

    monkeypatch.setattr(suz, "sign_in",
                        lambda *args, **kwargs: "свежий-токен-от-суз")
    suz.save(FULL, Contour.PRODUCTION)

    answer, used = service.with_fresh_token(FULL, Contour.PRODUCTION, "ААББ", "",
                                            call)

    assert answer == "получилось"
    assert tries == [TOKEN, "свежий-токен-от-суз"]
    assert used.token == "свежий-токен-от-суз"
    # Свежий токен сохранён: иначе следующий запуск снова начал бы с отказа.
    assert suz.load(Contour.PRODUCTION).token == "свежий-токен-от-суз"


def test_без_сертификата_обновлять_нечем(monkeypatch):
    from app.core.marking import service

    def call(credentials):
        raise transport.MarkingError("Проверка учетных данных УОТ", 400)

    with pytest.raises(transport.MarkingError):
        service.with_fresh_token(FULL, Contour.SANDBOX, "", "", call)


def test_отказ_не_по_токену_не_дёргает_ключ(monkeypatch):
    """Пароль к контейнеру не спрашивают из-за ошибки, которую он не лечит."""
    from app.core.marking import service

    asked = []
    monkeypatch.setattr(suz, "sign_in",
                        lambda *a, **k: asked.append(1) or "новый")

    def call(credentials):
        raise transport.MarkingError("Метод не поддерживается", 405)

    with pytest.raises(transport.MarkingError):
        service.with_fresh_token(FULL, Contour.SANDBOX, "ААББ", "", call)

    assert asked == []


def test_обновление_ровно_одно(monkeypatch):
    """Если и свежий токен отвергнут, дело не в сроке, и крутить цикл незачем."""
    from app.core.marking import service

    tries = []

    def call(credentials):
        tries.append(credentials.token)
        raise transport.MarkingError("Проверка учетных данных УОТ", 400)

    monkeypatch.setattr(suz, "sign_in", lambda *a, **k: "свежий")

    with pytest.raises(transport.MarkingError):
        service.with_fresh_token(FULL, Contour.SANDBOX, "ААББ", "", call)

    assert len(tries) == 2
