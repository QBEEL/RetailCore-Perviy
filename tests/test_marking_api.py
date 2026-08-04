"""Маркировка: транспорт, вход, журнал операций и проверка кодов.

Сеть здесь не трогается: транспорт подменяется, а часы и сон передаются
снаружи — иначе проверка выдержки темпа шла бы в реальном времени.
"""
from __future__ import annotations

import pytest

from app.core.marking import (
    BATCH_SIZE,
    CodeState,
    Contour,
    OperationKind,
    OperationStatus,
    fingerprint,
    group_of,
    parse_state,
)
from app.core.marking import service, session, store, transport
from app.core.marking.models import Operation
from app.core.marking.transport import Limiter


# --- удержание темпа ------------------------------------------------------------

class Clock:
    """Часы, которыми управляет тест."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_limiter_lets_the_whole_quota_through_at_once():
    """Квота тратится пачкой, а не размазывается паузами между запросами."""
    clock = Clock()
    limiter = Limiter(limit=5, window=60.0, clock=clock.time, sleep=clock.sleep)

    for _ in range(5):
        limiter.take()

    assert clock.slept == []


def test_limiter_waits_when_quota_is_spent():
    clock = Clock()
    limiter = Limiter(limit=3, window=60.0, clock=clock.time, sleep=clock.sleep)
    for _ in range(3):
        limiter.take()

    limiter.take()

    assert clock.slept, "четвёртый запрос обязан был выждать"
    assert clock.now >= 60.0


def test_limiter_forgets_old_marks():
    """Окно скользящее: как только оно проехало, квота свободна снова."""
    clock = Clock()
    limiter = Limiter(limit=2, window=60.0, clock=clock.time, sleep=clock.sleep)
    limiter.take()
    limiter.take()
    clock.now += 61.0

    limiter.take()

    assert clock.slept == []


# --- адреса ---------------------------------------------------------------------

def test_unknown_contour_refuses_instead_of_guessing():
    """Незаполненный адрес обязан отказать, а не увести запрос в бой."""
    transport.configure("trueapi", Contour.SANDBOX, "")
    try:
        with pytest.raises(transport.MarkingError, match="Не задан адрес"):
            transport.url("trueapi", "/x", Contour.SANDBOX)
    finally:
        transport.configure("trueapi", Contour.SANDBOX,
                            "https://markirovka.sandbox.crptech.ru")


def test_production_and_sandbox_are_different_hosts():
    live = transport.url("trueapi", "/auth/cert/key", Contour.PRODUCTION)
    test = transport.url("trueapi", "/auth/cert/key", Contour.SANDBOX)

    assert live != test
    assert "sandbox" in test


def test_two_systems_have_their_own_prefixes():
    """ГИС МТ и True API — разные системы, а не разные пути одной.

    True API живёт под третьей версией в собственном разделе: по `/api/v4` оба
    хоста отвечают 403. Адреса проверены обращением, а не взяты из описания.
    """
    ismp = transport.url("ismp", "/auth/cert/key", Contour.PRODUCTION)
    true_api = transport.url("trueapi", "/auth/key", Contour.PRODUCTION)

    assert ismp == "https://ismp.crpt.ru/api/v3/auth/cert/key"
    assert true_api == "https://markirovka.crpt.ru/api/v3/true-api/auth/key"


# --- ошибки ---------------------------------------------------------------------

def test_401_asks_for_a_new_sign_in():
    error = transport._failure(401)

    assert isinstance(error, transport.AuthRequired)
    assert error.status == 401


def test_429_is_told_apart_from_other_refusals():
    error = transport._failure(429)

    assert isinstance(error, transport.RateLimited)


def test_error_text_is_taken_from_the_answer():
    error = transport._failure(400, b'{"error_message": "\xd0\x9a\xd0\xbe\xd0\xb4"}')

    assert "Код" in str(error)


def test_retries_do_not_repeat_a_rejected_document(monkeypatch):
    """Четырёхсотые не повторяются: принятый повтор — вторая операция."""
    calls = []

    def refuse(prepared, tolerate=()):
        calls.append(prepared)
        raise transport.MarkingError("отказано", 400)

    monkeypatch.setattr(transport, "_send", refuse)
    transport.limiter.reset()
    with pytest.raises(transport.MarkingError):
        transport.request("POST", "trueapi", "/x", contour=Contour.SANDBOX,
                          body={}, sleep=lambda _: None)

    assert len(calls) == 1


def test_server_errors_are_retried(monkeypatch):
    calls = []

    def stumble(prepared, tolerate=()):
        calls.append(prepared)
        if len(calls) < 3:
            raise transport.MarkingError("сервер прилёг", 503)
        return {"ok": True}

    monkeypatch.setattr(transport, "_send", stumble)
    transport.limiter.reset()
    answer = transport.request("GET", "trueapi", "/x", contour=Contour.SANDBOX,
                               sleep=lambda _: None)

    assert answer == {"ok": True}
    assert len(calls) == 3


# --- вход -----------------------------------------------------------------------

def _fake_certificate(monkeypatch):
    monkeypatch.setattr(session.crypto, "find",
                        lambda t: session.crypto.Certificate(
                            thumbprint=t, subject="CN=Иванов, ИНН=123",
                            has_private_key=True))


def test_sign_in_signs_the_string_and_names_the_organisation(monkeypatch):
    """Вход — это подпись присланной строки плюс ИНН, за кого действуем."""
    signed, posted = {}, {}
    _fake_certificate(monkeypatch)

    def sign(data, thumbprint):
        signed["data"] = data
        return "подпись-base64"

    monkeypatch.setattr(session.crypto, "sign", sign)
    monkeypatch.setattr(session.transport, "get",
                        lambda *a, **k: {"uuid": "u-1", "data": "случайная-строка"})

    def post(system, path, **kwargs):
        posted.update(path=path, body=kwargs.get("body"))
        return {"token": "т-42"}

    monkeypatch.setattr(session.transport, "post", post)

    result = session.sign_in("ABC", "250101802801", Contour.SANDBOX, "ИП Саух")

    assert signed["data"] == "случайная-строка"
    assert posted["path"] == "/auth/simpleSignIn"
    assert posted["body"]["inn"] == "250101802801"
    assert result.token == "т-42"
    assert result.organisation == "ИП Саух"
    session.sign_out()


def test_several_organisations_ask_which_one():
    """Сертификат по МЧД действует за несколько лиц — сервер требует выбрать."""
    with pytest.raises(transport.MarkingError, match="несколько организаций"):
        session._token({"mchdUser": True, "organisations": [
            {"inn": "1", "fullName": "ИП Первый"},
            {"inn": "2", "fullName": "ИП Второй"}]})


def test_organisations_come_from_gismt(monkeypatch):
    """Список, за кого действует сертификат, отдаёт ГИС МТ, а не True API."""
    _fake_certificate(monkeypatch)
    monkeypatch.setattr(session.crypto, "sign", lambda d, t: "подпись")
    monkeypatch.setattr(session.transport, "get",
                        lambda *a, **k: {"uuid": "u", "data": "строка"})
    monkeypatch.setattr(session.transport, "post", lambda *a, **k: {
        "mchdUser": True,
        "organisations": [{"inn": "250101802801", "fullName": "САУХ ИВАН"}]})

    found = session.organisations("ABC", Contour.PRODUCTION)

    assert [(o.inn, o.name) for o in found] == [("250101802801", "САУХ ИВАН")]


def test_token_expiry_survives_a_silent_server(monkeypatch):
    """Срок жизни токена в описаниях расходится — своё значение обязано быть."""
    from datetime import datetime

    without = session._expiry({"token": "x"})

    assert without > datetime.now()


def test_expiry_uses_the_server_value_when_given():
    from datetime import datetime, timedelta

    given = session._expiry({"expires_in": 36000})

    assert given > datetime.now() + timedelta(hours=9)


def test_expired_token_is_refused_without_a_certificate():
    session.sign_out()
    with pytest.raises(transport.AuthRequired):
        session.token()


def test_401_triggers_exactly_one_retry(monkeypatch):
    attempts = []

    monkeypatch.setattr(session, "token", lambda system="trueapi": "т")

    def always_denied(token):
        attempts.append(token)
        raise transport.AuthRequired("протух", 401)

    with pytest.raises(transport.AuthRequired):
        session.authorized(always_denied)

    assert len(attempts) == 2, "повтор ровно один, цикл крутить незачем"


# --- журнал операций --------------------------------------------------------------

def _operation(codes=("код-1", "код-2"), kind=OperationKind.WITHDRAWAL):
    return Operation(kind=kind, codes=list(codes), contour=Contour.SANDBOX)


def test_operation_is_written_before_it_is_sent(tmp_path):
    """Сначала запись, потом ГИС МТ: обрыв не должен стереть след."""
    path = str(tmp_path / "marking.db")
    saved = store.create(_operation(), path)

    assert saved.id
    assert store.get(saved.id, path).status is OperationStatus.DRAFT


def test_sent_operation_cannot_be_deleted(tmp_path):
    """Запись — единственное свидетельство, что документ в ГИС МТ существует."""
    path = str(tmp_path / "marking.db")
    saved = store.create(_operation(), path)
    saved.status = OperationStatus.SENT
    store.update(saved, path)

    with pytest.raises(ValueError, match="удалить нельзя"):
        store.delete(saved.id, path)


def test_failed_operation_can_be_deleted(tmp_path):
    path = str(tmp_path / "marking.db")
    saved = store.create(_operation(), path)
    saved.status = OperationStatus.FAILED
    store.update(saved, path)

    store.delete(saved.id, path)

    assert store.get(saved.id, path) is None


def test_sending_is_not_resendable():
    """Обрыв на отправке не значит, что сервер её не принял."""
    assert not OperationStatus.SENDING.resendable
    assert not OperationStatus.SENT.resendable
    assert OperationStatus.FAILED.resendable
    assert OperationStatus.DRAFT.resendable


def test_unknown_status_is_treated_as_pending(tmp_path):
    """Неизвестное состояние безопаснее считать незавершённым."""
    path = str(tmp_path / "marking.db")
    saved = store.create(_operation(), path)
    with store.connect(path) as connection:
        connection.execute("UPDATE operation SET status = 'выдумка' WHERE id = ?",
                           (saved.id,))
        connection.commit()

    assert store.get(saved.id, path).status is OperationStatus.SENT


def test_same_codes_are_noticed_as_a_repeat(tmp_path):
    """Второй запуск того же набора — почти всегда второе нажатие."""
    path = str(tmp_path / "marking.db")
    first = store.create(_operation(), path)
    first.status = OperationStatus.SENT
    store.update(first, path)

    found = store.duplicates(["код-2", "код-1"], OperationKind.WITHDRAWAL, path)

    assert [item.id for item in found] == [first.id]


def test_repeat_check_ignores_order_and_kind(tmp_path):
    path = str(tmp_path / "marking.db")
    saved = store.create(_operation(), path)
    saved.status = OperationStatus.SENT
    store.update(saved, path)

    assert not store.duplicates(["код-1", "код-2"], OperationKind.SHIP, path)
    assert not store.duplicates(["код-3"], OperationKind.WITHDRAWAL, path)


def test_fingerprint_ignores_order_and_duplicates():
    assert fingerprint(["b", "a"]) == fingerprint(["a", "b", "a"])
    assert fingerprint(["a"]) != fingerprint(["b"])


def test_pending_operations_are_listed_for_polling(tmp_path):
    path = str(tmp_path / "marking.db")
    waiting = store.create(_operation(codes=["к-1"]), path)
    waiting.status = OperationStatus.SENT
    store.update(waiting, path)
    finished = store.create(_operation(codes=["к-2"]), path)
    finished.status = OperationStatus.DONE
    store.update(finished, path)

    assert [item.id for item in store.pending(path)] == [waiting.id]


def test_polling_does_nothing_until_it_is_configured(tmp_path):
    """Пока опрос не подключён, судьба операции не угадывается."""
    path = str(tmp_path / "marking.db")
    saved = store.create(_operation(), path)
    saved.status = OperationStatus.SENT
    store.update(saved, path)

    assert service.refresh_pending(path) == []
    assert store.get(saved.id, path).status is OperationStatus.SENT


# --- проверка кодов ----------------------------------------------------------------

def test_codes_are_split_into_batches(monkeypatch, tmp_path):
    """Тысяча кодов на запрос — потолок, проверенный ответом сервера."""
    sent: list[list[str]] = []

    def ask(batch, contour):
        sent.append(list(batch))
        return [_fake_info(code) for code in batch]

    monkeypatch.setattr(service, "_ask", ask)
    codes = [f"код-{n}" for n in range(BATCH_SIZE * 2 + 20)]

    service.check(codes, contour=Contour.SANDBOX, use_cache=False,
                  db_path=str(tmp_path / "m.db"))

    assert [len(part) for part in sent] == [BATCH_SIZE, BATCH_SIZE, 20]


def _fake_info(code: str):
    from app.core.marking.models import CodeInfo

    return CodeInfo(code=code, found=True, valid=True, state=CodeState.INTRODUCED)


def test_missing_code_in_the_answer_is_not_treated_as_fine():
    """Код ушёл в запрос, а в ответе его нет — это повод насторожиться."""
    result = service._parse_check({"codes": []}, ["код-1"])

    assert len(result) == 1
    assert not result[0].found
    assert result[0].problems


def test_answer_shape_may_be_a_list_or_an_object():
    as_list = service._parse_check([{"cis": "к", "status": "INTRODUCED"}], ["к"])
    as_object = service._parse_check(
        {"codes": [{"cis": "к", "status": "INTRODUCED"}]}, ["к"])

    assert as_list[0].state is CodeState.INTRODUCED
    assert as_object[0].state is CodeState.INTRODUCED


def test_unknown_state_does_not_break_the_answer():
    """Система добавляет состояния — приложение обязано это переживать."""
    assert parse_state("НЕЧТО_НОВОЕ") is CodeState.UNKNOWN
    assert parse_state("INTRODUCED") is CodeState.INTRODUCED
    assert parse_state("in_circulation") is CodeState.INTRODUCED


def test_check_results_are_remembered(tmp_path):
    path = str(tmp_path / "marking.db")
    from app.core.marking.models import CodeInfo

    store.remember_checks(
        [CodeInfo(code="к-1", gtin="0" * 14, state=CodeState.INTRODUCED,
                  valid=True, found=True)], Contour.SANDBOX, path)

    known = store.known_checks(["к-1"], Contour.SANDBOX, path)

    assert known["к-1"].state is CodeState.INTRODUCED


def test_cache_does_not_mix_contours(tmp_path):
    """Один код в песочнице и в бою — разные вещи."""
    path = str(tmp_path / "marking.db")
    from app.core.marking.models import CodeInfo

    store.remember_checks([CodeInfo(code="к-1", found=True)], Contour.SANDBOX, path)

    assert store.known_checks(["к-1"], Contour.PRODUCTION, path) == {}


def test_scanner_noise_is_stripped_before_asking():
    """Сканер добавляет признак символики — в запрос он уйти не должен."""
    prepared = service._prepare(["]d2010460123456789021ABC", "]d2010460123456789021ABC"])

    assert len(prepared) == 1
    assert not prepared[0].startswith("]d2")


# --- модель операции ------------------------------------------------------------------

def test_withdrawal_is_marked_irreversible():
    assert not OperationKind.WITHDRAWAL.reversible
    assert not OperationKind.REMARK.reversible
    assert OperationKind.CHECK.reversible
    assert OperationKind.CHECK.read_only


def test_danger_is_about_production_only():
    """В песочнице подтверждение не нужно — там ошибка ничего не стоит."""
    live = Operation(kind=OperationKind.WITHDRAWAL, contour=Contour.PRODUCTION)
    test = Operation(kind=OperationKind.WITHDRAWAL, contour=Contour.SANDBOX)

    assert live.dangerous
    assert not test.dangerous


def test_batch_count_matches_the_limit():
    operation = Operation(codes=[f"к-{n}" for n in range(BATCH_SIZE * 2 + 1)])

    assert operation.batches == 3


def test_product_group_is_data_not_a_constant():
    assert group_of("perfumery").title == "Духи и туалетная вода"
    assert group_of("выдумка") is None


# --- то, что выяснено обращением к живому API ------------------------------------

def test_signature_is_attached():
    """Отсоединённую подпись система маркировки не принимает.

    Проверено на обоих контурах: с `detached=True` ответ «Подпись невалидна.
    Код ошибки: 2» при любых прочих параметрах, с присоединённой — токен.
    Значение вынесено константой, чтобы правка не прошла незамеченной.
    """
    from app.core.marking import crypto

    assert crypto._ATTACHED is False


def test_not_found_answer_is_a_result_not_an_error():
    """У сведений о кодах ненайденный код делает ответ целиком 404.

    Тело при этом полноценное, и «часть кодов не найдена» — это и есть
    полезный результат проверки.
    """
    assert 404 in service.CHECK_TOLERATE


def test_check_body_is_a_bare_list(monkeypatch):
    """На объект сервер отвечает «Cannot deserialize value of type ArrayList»."""
    sent = {}

    def post(system, path, **kwargs):
        sent.update(path=path, body=kwargs.get("body"),
                    tolerate=kwargs.get("tolerate"))
        return []

    monkeypatch.setattr(service.transport, "post", post)
    monkeypatch.setattr(service.session, "authorized", lambda call: call("т"))

    service._ask(["к-1", "к-2"], Contour.PRODUCTION)

    assert sent["path"] == "/cises/info"
    assert sent["body"] == ["к-1", "к-2"]
    assert 404 in sent["tolerate"]


def test_answer_puts_details_inside_cis_info():
    """Сведения лежат вложенным объектом, а причина отказа — рядом с ним."""
    result = service._parse_check([{
        "cisInfo": {"requestedCis": "к-1", "cis": "к-1", "gtin": "",
                    "productGroup": None},
        "errorMessage": "КМ/КИ не найден",
        "errorCode": "404",
    }], ["к-1"])

    assert len(result) == 1
    assert not result[0].found
    assert "не найден" in result[0].problems[0]
