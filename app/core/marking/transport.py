"""Связь с ГИС МТ, True API и СУЗ: адреса, лимиты, повторы, ошибки.

Систем три, и это не деталь, а устройство: документами оборота заведует ГИС МТ,
проверкой кодов — True API, а выдачей кодов — СУЗ. Адреса у них разные, и
токены тоже разные, поэтому хост выбирается явно на каждом запросе.

Различается и способ предъявить токен: ГИС МТ с True API ждут его заголовком
`Authorization: Bearer`, а СУЗ — собственным заголовком `clientToken`. Отсюда
два параметра вместо одного: `token` для первых двух, `headers` для третьей.

Лимиты здесь — не оптимизация, а основа: тысяча кодов на запрос и полсотни
запросов в секунду от одного участника. Держать темп обязан сам транспорт —
выше по коду никто не считает, сколько запросов ушло за последнюю секунду.

Ещё одна особенность, ради которой появился `tolerate`: у метода сведений о
кодах ненайденный код делает ответ целиком четырёхсоточетвёртым, хотя тело при
этом полноценное и содержит разбор по каждому коду. Считать такой ответ ошибкой
нельзя — «часть кодов не найдена» это и есть полезный результат проверки.

Как и в оплатах, используется `urllib` из стандартной библиотеки: приложение
собирается в один exe, и каждая новая зависимость — это лишние мегабайты и ещё
одна библиотека, которую придётся обновлять при находке уязвимости.
"""
from __future__ import annotations

import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any, Callable

from .. import net
from .models import Contour, REQUESTS_PER_SECOND

TIMEOUT = 60

# Адреса. Все шесть проверены обращением: `/auth/key` и `/auth/cert/key`
# отвечают на первых четырёх, `/api/v3/ping` — на обоих адресах СУЗ.
HOSTS: dict[tuple[str, Contour], str] = {
    ("ismp", Contour.PRODUCTION): "https://ismp.crpt.ru",
    ("ismp", Contour.SANDBOX): "https://markirovka.sandbox.crptech.ru",
    ("trueapi", Contour.PRODUCTION): "https://markirovka.crpt.ru",
    ("trueapi", Contour.SANDBOX): "https://markirovka.sandbox.crptech.ru",
    ("suz", Contour.PRODUCTION): "https://suzgrid.crpt.ru",
    ("suz", Contour.SANDBOX): "https://suz.sandbox.crptech.ru",
}


# Префикс пути у каждой системы свой. True API живёт под третьей версией, а не
# под четвёртой: по `/api/v4` оба хоста отвечают 403.
PREFIX = {"ismp": "/api/v3", "trueapi": "/api/v3/true-api", "suz": "/api/v3"}


def configure(system: str, contour: Contour, host: str) -> None:
    """Задаёт адрес системы. Пустое значение убирает его совсем.

    Убрать адрес — законное действие: незаполненный контур должен отказываться
    работать с внятным объяснением, а не молча уводить запрос в бой.
    """
    key = (system, contour)
    if host.strip():
        HOSTS[key] = host.strip().rstrip("/")
    else:
        HOSTS.pop(key, None)

# Сколько ждать между повторами. Числа растут: сервер, ответивший 429, не
# станет добрее через сто миллисекунд.
RETRY_DELAYS = (1.0, 3.0, 8.0)


class MarkingError(Exception):
    """Ошибка обращения к системе маркировки. Текст пригоден для показа."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class AuthRequired(MarkingError):
    """Токен не получен, истёк или отозван. Нужен повторный вход по сертификату."""


class Offline(MarkingError):
    """До сервера не достучались. Данные не изменились — можно повторить."""


class RateLimited(MarkingError):
    """Превышен лимит запросов. Внутри транспорта обрабатывается сам."""


class Limiter:
    """Удержание темпа: не больше `limit` запросов за `window` секунд.

    Скользящее окно, а не «поспать между запросами»: лимит задан на секунду —
    пятьдесят запросов от одного участника, — и укладываться в него пачкой, а
    потом ждать, ровно то поведение, которого ждёт сервер.

    Часы передаются снаружи, чтобы тесты проверяли выдержку, не проводя минуту
    в ожидании.
    """

    def __init__(self, limit: int = REQUESTS_PER_SECOND, window: float = 1.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._sleep = sleep
        self._marks: deque[float] = deque()
        self._lock = threading.Lock()

    def take(self) -> float:
        """Разрешает очередной запрос, при необходимости выждав. Вернёт паузу."""
        while True:
            with self._lock:
                now = self._clock()
                while self._marks and now - self._marks[0] >= self.window:
                    self._marks.popleft()
                if len(self._marks) < self.limit:
                    self._marks.append(now)
                    return 0.0
                pause = self.window - (now - self._marks[0])
            # Сон вне блокировки: иначе соседний поток простоит всю паузу,
            # даже если к его очереди место уже освободится.
            self._sleep(max(pause, 0.01))

    def reset(self) -> None:
        with self._lock:
            self._marks.clear()


# Один ограничитель на процесс: лимит считает сервер, и делить его между
# фоновыми задачами приложения бессмысленно.
limiter = Limiter()


def url(system: str, path: str, contour: Contour,
        params: dict[str, Any] | None = None) -> str:
    """Полный адрес запроса. Неизвестный контур — ошибка, а не боевой адрес."""
    host = HOSTS.get((system, contour), "")
    if not host:
        raise MarkingError(
            f"Не задан адрес {system} для контура «{contour.title}». "
            "Укажите его в настройках маркировки.")
    address = host + PREFIX.get(system, "") + path
    if not params:
        return address
    pairs: list[tuple[str, str]] = []
    for name, value in params.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((name, str(item)) for item in value)
        elif isinstance(value, bool):
            pairs.append((name, "true" if value else "false"))
        else:
            pairs.append((name, str(value)))
    return f"{address}?{urllib.parse.urlencode(pairs)}" if pairs else address


def request(
    method: str,
    system: str,
    path: str,
    *,
    contour: Contour = Contour.SANDBOX,
    params: dict[str, Any] | None = None,
    body: Any = None,
    text_body: str | None = None,
    token: str = "",
    headers: dict[str, str] | None = None,
    retries: int = len(RETRY_DELAYS),
    tolerate: tuple[int, ...] = (),
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Запрос с удержанием темпа и повторами.

    Повторяются только те отказы, после которых состояние на сервере заведомо
    не изменилось: недоступность, 429 и пятисотые. Четырёхсотые, кроме 429, не
    повторяются никогда — на отклонённый документ сервер ответит так же, а вот
    принятый повтор означал бы вторую операцию с теми же кодами.

    `tolerate` перечисляет коды ответа, при которых тело возвращается как
    обычный результат, а не превращается в исключение.

    `headers` добавляет свои заголовки — этим пользуется СУЗ, которая ждёт
    токен в `clientToken`, а не в `Authorization`.

    `text_body` отправляет готовую строку вместо того, чтобы собирать её здесь.
    Нужен там, где тело подписано: подпись считается по конкретным байтам, и
    собрать их второй раз — значит однажды собрать иначе и получить «подпись
    невалидна» без единой подсказки, почему.
    """
    data, sending = None, {"Accept": "application/json"}
    if text_body is not None:
        data = text_body.encode("utf-8")
        sending["Content-Type"] = "application/json"
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False, default=_encode).encode("utf-8")
        sending["Content-Type"] = "application/json"
    if token:
        sending["Authorization"] = f"Bearer {token}"
    sending.update(headers or {})

    address = url(system, path, contour, params)
    attempt = 0
    while True:
        limiter.take()
        try:
            return _send(urllib.request.Request(
                address, data=data, headers=sending, method=method), tolerate)
        except (Offline, RateLimited):
            if attempt >= retries:
                raise
            sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
            attempt += 1
        except MarkingError as error:
            if error.status < 500 or attempt >= retries:
                raise
            sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
            attempt += 1


def _send(prepared: urllib.request.Request,
          tolerate: tuple[int, ...] = ()) -> Any:
    try:
        with urllib.request.urlopen(prepared, timeout=TIMEOUT, context=net.context) as response:
            payload = response.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as error:
        # Тело читается один раз: повторный `read()` вернёт пустоту, и разбор
        # ответа с терпимым кодом сломался бы об это.
        try:
            payload = error.read()
        except (AttributeError, ValueError, OSError):
            payload = b""
        if error.code in tolerate:
            try:
                return json.loads(payload) if payload else None
            except json.JSONDecodeError:
                pass
        raise _failure(error.code, payload) from None
    except UnicodeEncodeError:
        # Заголовки уходят latin-1, и русская буква в токене роняет `urllib`
        # ещё до отправки. Само по себе это исключение говорит только про
        # «position 0-1» — толку от него человеку никакого.
        raise MarkingError(
            "В заголовке запроса оказались русские буквы. Так бывает, когда "
            "токен набран в русской раскладке или вставлен вместе с лишним "
            "текстом: в нём только латиница, цифры и знаки.") from None
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as error:
        raise Offline(
            "Система маркировки недоступна. Проверьте подключение к сети."
            f"\n\nПодробности: {error}") from None
    except json.JSONDecodeError as error:
        raise MarkingError(
            "Система маркировки ответила не JSON — вероятно, обращение ушло "
            f"не туда. Подробности: {error}") from None


def _failure(code: int, payload: bytes = b"") -> MarkingError:
    """Ответ об ошибке → исключение с человеческим текстом."""
    detail = ""
    try:
        answer = json.loads(payload) if payload else None
    except (ValueError, TypeError):
        answer = None
    if isinstance(answer, dict):
        # У ГИС МТ текст ошибки лежит под разными именами в зависимости от
        # метода, поэтому перебираем известные.
        for name in ("error_message", "errorMessage", "description", "detail",
                     "message", "error"):
            if value := answer.get(name):
                detail = str(value)
                break
        # У СУЗ он лежит списком, а не строкой, и списка два. `globalErrors` —
        # про запрос целиком: {"errorCode":1110,"error":"…"}. `fieldErrors` —
        # про отдельное поле, и устроен иначе:
        # {"errorCode":6010,"fieldError":"…","fieldName":"reportId"}.
        #
        # Читать только первый — значит на разборе по полям показать человеку
        # «Система маркировки отвергла запрос» вместо готового ответа, какого
        # именно поля не хватает. Сервер сказал, а мы потеряли.
        if not detail:
            detail = _listed(answer.get("globalErrors"))
        if not detail:
            detail = _listed(answer.get("fieldErrors"))
    if code == 401:
        return AuthRequired(
            detail or "Требуется вход по сертификату — токен истёк или отозван.",
            401)
    if code == 429:
        return RateLimited(
            detail or "Превышен лимит обращений к системе маркировки.", 429)
    if code == 400 and not detail:
        detail = "Система маркировки отвергла запрос."
    return MarkingError(detail or f"Система маркировки ответила кодом {code}", code)


def _listed(errors: Any) -> str:
    """Список ошибок СУЗ → одна строка.

    Код ошибки сохраняется — по нему ищут в поддержке; имя поля тоже, иначе
    «Не указано значение параметра» не говорит, какого именно.
    """
    if not isinstance(errors, list):
        return ""
    texts = []
    for item in errors:
        if not isinstance(item, dict):
            continue
        text = str(item.get("error") or item.get("fieldError")
                   or item.get("errorMessage") or "").strip()
        if not text:
            continue
        if name := item.get("fieldName"):
            text = f"{text} [{name}]"
        if code := item.get("errorCode"):
            text = f"{text} (код {code})"
        texts.append(text)
    return " ".join(texts)


def _encode(value: Any) -> str:
    """Даты и время в JSON — строкой ISO."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} не сериализуется")


def get(system: str, path: str, *, contour: Contour = Contour.SANDBOX,
        params: dict | None = None, token: str = "",
        headers: dict[str, str] | None = None,
        tolerate: tuple[int, ...] = ()) -> Any:
    return request("GET", system, path, contour=contour, params=params,
                   token=token, headers=headers, tolerate=tolerate)


def post(system: str, path: str, *, contour: Contour = Contour.SANDBOX,
         params: dict | None = None, body: Any = None,
         text_body: str | None = None, token: str = "",
         headers: dict[str, str] | None = None,
         retries: int = len(RETRY_DELAYS),
         tolerate: tuple[int, ...] = ()) -> Any:
    """Отправка. `retries=0` — для того, что нельзя повторять вслепую.

    Повтор безопасен, пока запрос ничего не создаёт. Заказ кодов создаёт, и
    оборванный ответ на него не означает «не дошло»: второй такой запрос — это
    второй заказ и вторые деньги.
    """
    return request("POST", system, path, contour=contour, params=params,
                   body=body, text_body=text_body, token=token, headers=headers,
                   retries=retries, tolerate=tolerate)
