"""Сценарии маркировки: вход, проверка кодов, журнал операций, реквизиты СУЗ.

Интерфейс вызывает только эти функции — они рассчитаны на работу из фоновой
задачи и сообщают о ходе через `progress`. Проверка кодов ничего не меняет в
ГИС МТ, поэтому она сделана первой: на ней обкатываются подпись, токен и
удержание темпа, а цена ошибки нулевая.

Разбор ответа вынесен в `_info`: состав полей у разных товарных групп
отличается, и вся разница собрана в одном месте, а не размазана по коду.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable, Sequence

from . import codes as codes_module
from . import crypto, orders as orders_module, session, store, suz, transport
from .models import (
    BATCH_SIZE,
    CodeInfo,
    CodeState,
    Contour,
    Operation,
    OperationKind,
    OperationStatus,
    ProductGroup,
    parse_state,
)

Progress = Callable[[int, int], None]

# Метод сведений о кодах True API. Тело запроса — голый массив строк, а не
# объект: на объект сервер отвечает «Cannot deserialize value of type ArrayList».
CHECK_PATH = "/cises/info"

# Ненайденный код делает ответ целиком четырёхсоточетвёртым, хотя тело при этом
# полноценное: по каждому коду свой разбор. Это и есть результат проверки —
# считать его ошибкой нельзя.
CHECK_TOLERATE = (404,)


def certificates() -> list[crypto.Certificate]:
    """Личные сертификаты. Пустой список означает, что подписывать нечем."""
    if not crypto.available():
        return []
    return crypto.certificates()


def crypto_available() -> bool:
    return crypto.available()


def organisations(thumbprint: str,
                  contour: Contour = Contour.SANDBOX) -> list[session.Organisation]:
    """За кого может действовать сертификат. Обращается к закрытому ключу."""
    return session.organisations(thumbprint, contour)


def sign_in(thumbprint: str, inn: str = "",
            contour: Contour = Contour.SANDBOX,
            organisation: str = "") -> session.Session:
    """Вход по сертификату. Может открыть окно ввода пароля к ключу.

    `inn` обязателен, если сертификат действует за несколько организаций —
    список берётся из `organisations`.
    """
    return session.sign_in(thumbprint, inn, contour, organisation)


def needs_organisation(error: object) -> bool:
    """Отказ ли это из-за невыбранной организации.

    Нужно интерфейсу: на такой отказ он открывает выбор, а не показывает
    пользователю сообщение, из которого непонятно, что делать дальше.
    """
    return session.needs_organisation(error)


def sign_out() -> None:
    session.sign_out()


def signed_in() -> bool:
    return session.session.active


def current() -> session.Session:
    return session.session


# --- станция управления заказами -----------------------------------------------

def suz_credentials(contour: Contour = Contour.SANDBOX) -> suz.Credentials:
    """Сохранённые реквизиты СУЗ этого контура."""
    return suz.load(contour)


def save_suz(credentials: suz.Credentials,
             contour: Contour = Contour.SANDBOX) -> suz.Credentials:
    return suz.save(credentials, contour)


def forget_suz(contour: Contour = Contour.SANDBOX) -> None:
    suz.forget(contour)


def suz_ready(contour: Contour = Contour.SANDBOX) -> bool:
    """Заполнены ли реквизиты. О том, приняты ли они, говорит только проверка."""
    return suz.known(contour)


def suz_sign_in(credentials: suz.Credentials, thumbprint: str, inn: str = "",
                contour: Contour = Contour.SANDBOX) -> suz.Credentials:
    """Получает токен СУЗ по сертификату и возвращает реквизиты с ним.

    Обращается к закрытому ключу — КриптоПро может спросить пароль к
    контейнеру, поэтому вызывается из фоновой задачи. Сохранение остаётся за
    интерфейсом: он же решает, что делать с полученным токеном.
    """
    return suz.renewed(credentials, suz.sign_in(thumbprint, inn, contour,
                                                credentials.connection_id))


def with_fresh_token(
    credentials: suz.Credentials,
    contour: Contour,
    thumbprint: str,
    inn: str,
    call: Callable[[suz.Credentials], Any],
) -> tuple[Any, suz.Credentials]:
    """Выполняет обращение к СУЗ, переживая протухший токен.

    Токен СУЗ живёт десять часов, и до сих пор его продление означало сходить в
    другую программу и нажать там «Тест». Это не работа человека: сертификат у
    нас есть, вход по нему устроен, и добыть новый токен приложение может само.

    Обновление одно: если и со свежим токеном отказ, дело не в сроке, а в
    правах или реквизитах, и крутить цикл незачем. На отказ по учётным данным
    сервер запроса не исполнил, поэтому повтор не создаёт второй операции.

    Возвращается пара «результат и реквизиты, с которыми получилось»: токен мог
    смениться, и интерфейс обязан показать тот, что теперь настоящий.
    """
    try:
        return call(credentials), credentials
    except transport.MarkingError as error:
        if not thumbprint or not suz.stale(error):
            raise
    fresh = suz.save(
        suz.renewed(credentials, suz.sign_in(thumbprint, inn, contour,
                                             credentials.connection_id)),
        contour)
    return call(fresh), fresh


def check_suz(credentials: suz.Credentials,
              contour: Contour = Contour.SANDBOX, thumbprint: str = "",
              inn: str = "") -> tuple[str, suz.Credentials]:
    """Проверяет связь с СУЗ. Ничего не заказывает и не меняет.

    Обращается к сети, поэтому вызывается из фоновой задачи — как и вход по
    сертификату. С сертификатом заодно обновляет протухший токен.
    """
    return with_fresh_token(credentials, contour, thumbprint, inn,
                            lambda ready: suz.check(ready, contour))


def restore_suz() -> None:
    """Применяет сохранённые адреса СУЗ до первого обращения к ней."""
    suz.restore()


def suz_orders(credentials: suz.Credentials,
               contour: Contour = Contour.SANDBOX, thumbprint: str = "",
               inn: str = "") -> tuple[list[orders_module.Order], suz.Credentials]:
    """Заказы кодов и остаток в буферах. Ничего не создаёт и кодов не тратит."""
    return with_fresh_token(credentials, contour, thumbprint, inn,
                            lambda ready: orders_module.orders(ready, contour))


def create_suz_order(request: orders_module.Request, credentials: suz.Credentials,
                     contour: Contour = Contour.SANDBOX, thumbprint: str = "",
                     inn: str = "") -> tuple[str, suz.Credentials]:
    """Заводит заказ кодов. Единственная здесь операция, которая тратит деньги.

    Заказ подписывается сертификатом — СУЗ без подписи его не принимает, —
    поэтому `thumbprint` здесь не необязательный довесок, как в чтении, а
    условие работы.

    Повтора внутри нет ни одного — ни на уровне транспорта, ни здесь. Токен при
    этом обновиться может: отказ по учётным данным приходит до того, как заказ
    заведён, и повтор после него — это первая попытка, а не вторая.
    """
    return with_fresh_token(
        credentials, contour, thumbprint, inn,
        lambda ready: orders_module.create(ready, request, thumbprint, contour))


# Заказы читаются и заводятся. Признак остаётся: по нему вкладка отличает
# «пока нельзя» от «можно», и следующие методы СУЗ — получение кодов и отчёт о
# нанесении — будут добавляться так же.
ORDERING_READY = True


# --- проверка кодов ------------------------------------------------------------------

def check(
    raw_codes: Sequence[str],
    *,
    contour: Contour | None = None,
    use_cache: bool = True,
    progress: Progress | None = None,
    db_path: str | None = None,
) -> list[CodeInfo]:
    """Спрашивает у ГИС МТ, что она знает об этих кодах.

    Коды делятся на пачки по пятьдесят — это потолок одного запроса. Темп
    держит транспорт, здесь остаётся только показывать ход: на тысяче кодов
    это двадцать запросов и заметное время.

    `use_cache` подставляет ответы, полученные раньше. Он выключается, когда
    важна свежесть: состояние кода меняется, и вчерашний ответ про «в обороте»
    ничего не говорит о сегодняшнем.
    """
    where = contour or session.session.contour
    prepared = _prepare(raw_codes)
    if not prepared:
        return []

    known = store.known_checks(prepared, where, db_path) if use_cache else {}
    missing = [code for code in prepared if code not in known]
    fetched: dict[str, CodeInfo] = {}
    total = len(missing)
    done = 0
    if progress:
        progress(0, total)

    for start in range(0, total, BATCH_SIZE):
        batch = missing[start:start + BATCH_SIZE]
        for item in _ask(batch, where):
            fetched[item.code] = item
        done += len(batch)
        if progress:
            progress(min(done, total), total)

    if fetched:
        store.remember_checks(list(fetched.values()), where, db_path)

    inn = session.session.inn
    result: list[CodeInfo] = []
    for code in prepared:
        item = fetched.get(code) or known.get(code) or CodeInfo(code=code)
        item.our_inn = inn
        result.append(item)
    return result


def _prepare(raw_codes: Iterable[str]) -> list[str]:
    """Приводит коды к виду, в котором их ждёт ГИС МТ, и убирает повторы.

    Криптохвост здесь и отрезается: система ждёт код идентификации, а на код с
    ключом и значением проверки отвечает отказом. Заодно исчезают повторы,
    неразличимые в исходном виде: один и тот же экземпляр, снятый сканером
    дважды, приходит с одинаковым хвостом, а вставленный из таблицы — без него.

    Порядок сохраняется: пользователь сканировал в своём порядке и в таком же
    ожидает увидеть результат.
    """
    seen: set[str] = set()
    prepared: list[str] = []
    for raw in raw_codes:
        code = codes_module.for_request(str(raw or ""))
        if code and code not in seen:
            seen.add(code)
            prepared.append(code)
    return prepared


def _ask(batch: Sequence[str], contour: Contour) -> list[CodeInfo]:
    """Один запрос сведений. Пачка не длиннее тысячи кодов."""
    answer = session.authorized(
        lambda token: transport.post(
            "trueapi", CHECK_PATH, contour=contour,
            body=list(batch), token=token, tolerate=CHECK_TOLERATE))
    return _parse_check(answer, batch)


def _parse_check(answer: Any, batch: Sequence[str]) -> list[CodeInfo]:
    """Ответ проверки → список `CodeInfo`.

    Ответ приходит по-разному: то списком, то объектом со списком внутри под
    именем, зависящим от группы. Разбор терпит оба вида — иначе новая товарная
    группа ломала бы проверку целиком.
    """
    rows = _rows(answer)
    by_code: dict[str, CodeInfo] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = _info(row)
        if item.code:
            by_code[item.code] = item

    result: list[CodeInfo] = []
    for code in batch:
        item = by_code.get(code)
        if item is None:
            # Код ушёл в запрос, а в ответе его нет. Это не «всё хорошо»:
            # молча считать такой код исправным нельзя.
            item = CodeInfo(code=code, found=False)
            item.problems.append("система маркировки не ответила про этот код")
        result.append(item)
    return result


def _rows(answer: Any) -> list[Any]:
    if isinstance(answer, list):
        return answer
    if isinstance(answer, dict):
        for name in ("cises", "codes", "results", "data", "items"):
            value = answer.get(name)
            if isinstance(value, list):
                return value
    return []


def _info(row: dict) -> CodeInfo:
    """Одна запись ответа.

    Сведения о коде лежат вложенным объектом `cisInfo`, а признак неудачи —
    рядом с ним, на верхнем уровне. Оба слоя объединяются: искать поле сначала
    внутри, потом снаружи, — состав `cisInfo` по товарным группам отличается.
    """
    inner = row.get("cisInfo") if isinstance(row.get("cisInfo"), dict) else {}
    merged: dict = {**row, **inner}
    raw_code = _first(merged, ("cis", "requestedCis", "code", "km"))
    parsed = codes_module.parse(raw_code)
    item = CodeInfo(
        code=_first(merged, ("requestedCis", "cis", "code", "km")),
        gtin=_first(merged, ("gtin", "productGtin")) or parsed.gtin,
        serial=_first(merged, ("sgtin", "serialNumber", "serial")) or parsed.serial,
        state=parse_state(_first(merged, ("status", "state", "emissionState"))),
        owner_inn=_first(merged, ("ownerInn", "owner_inn", "inn")),
        owner_name=_first(merged, ("ownerName", "owner_name", "producerName")),
        product_name=_first(merged, ("productName", "product_name", "name")),
        raw=row,
    )
    error = _first(row, ("errorMessage", "error_message", "error"))
    code_of_error = _first(row, ("errorCode", "error_code"))
    item.found = _flag(merged, ("found", "isFound"),
                       default=not error and item.state is not CodeState.UNKNOWN)
    item.valid = _flag(merged, ("valid", "isValid", "verified"),
                       default=item.found and not error)
    if error:
        item.problems.append(f"{error} ({code_of_error})" if code_of_error else error)
    elif not item.valid:
        item.problems.append("код не признан системой маркировки")
    return item


def _first(row: dict, names: Sequence[str]) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def _flag(row: dict, names: Sequence[str], *, default: bool) -> bool:
    for name in names:
        if name in row:
            return bool(row[name])
    return default


# --- журнал операций --------------------------------------------------------------

def history(limit: int = 200, db_path: str | None = None) -> list[Operation]:
    return store.recent(limit, db_path)


def start(
    kind: OperationKind,
    raw_codes: Sequence[str],
    *,
    group: ProductGroup | str = "",
    contour: Contour | None = None,
    reason: str = "",
    comment: str = "",
    db_path: str | None = None,
) -> Operation:
    """Заводит операцию черновиком — до всякого обращения к ГИС МТ.

    Возвращается запись с уже присвоенным идентификатором. Отправка идёт
    отдельным шагом, и между ними интерфейс успевает показать пользователю,
    что именно уходит.
    """
    operation = Operation(
        kind=kind,
        codes=_prepare(raw_codes),
        product_group=group.code if isinstance(group, ProductGroup) else str(group or ""),
        contour=contour or session.session.contour,
        reason=reason,
        comment=comment,
        certificate=session.session.owner,
    )
    return store.create(operation, db_path)


def warn_duplicates(operation: Operation, db_path: str | None = None) -> list[Operation]:
    """Не запускалась ли уже такая операция с тем же набором кодов."""
    return store.duplicates(operation.codes, operation.kind, db_path)


def record_check(
    raw_codes: Sequence[str],
    *,
    contour: Contour | None = None,
    comment: str = "",
    db_path: str | None = None,
) -> Operation:
    """Отмечает выполненную проверку в журнале.

    Проверка ничего не меняет в ГИС МТ, поэтому ждать по ней ответа нечего:
    запись заводится и тут же закрывается. Журнал нужен ей ради другого — по
    нему видно, что и когда спрашивали, а следующая такая же пачка узнаётся
    `warn_duplicates` как повтор.
    """
    operation = start(OperationKind.CHECK, raw_codes, contour=contour,
                      comment=comment, db_path=db_path)
    operation.status = OperationStatus.DONE
    operation.checked_at = datetime.now()
    return store.update(operation, db_path)


# Опрос статуса документов пока не подключён: метод и состав ответа
# различаются по товарным группам, а подставить сюда догадку — значит однажды
# объявить выполненной операцию, которую ГИС МТ отвергла. Признак вынесен
# наружу, чтобы интерфейс говорил об этом прямо, а не изображал работу.
STATUS_POLLING_READY = False


def pending(db_path: str | None = None) -> list[Operation]:
    """Операции, ждущие ответа ГИС МТ."""
    return store.pending(db_path)


def refresh_pending(db_path: str | None = None,
                    progress: Progress | None = None) -> list[Operation]:
    """Опрашивает операции, ждущие ответа ГИС МТ.

    Вызывается при открытии вкладки и закрывает разрыв между «отправили» и
    «узнали результат»: приложение могли закрыть сразу после отправки, и без
    опроса операция висела бы в неизвестности вечно.

    Отправки здесь нет и быть не может — только чтение состояния. Пока опрос
    не подключён, функция честно ничего не делает: оставить операцию ждущей
    безопаснее, чем угадать её судьбу.
    """
    waiting = store.pending(db_path)
    if progress:
        progress(len(waiting), len(waiting))
    if not STATUS_POLLING_READY:
        return []
    raise NotImplementedError(
        "Опрос статуса включён, но не реализован — см. STATUS_POLLING_READY")


def forget_checks(db_path: str | None = None) -> int:
    return store.forget_checks(db_path)
