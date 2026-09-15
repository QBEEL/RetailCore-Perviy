"""Вход в систему маркировки по сертификату и жизнь токена.

Пароля здесь нет и быть не может: сервер выдаёт случайную строку, её нужно
подписать усиленной квалифицированной подписью и вернуть — в обмен на токен.
Поэтому вход стоит дорого в буквальном смысле: каждый требует обращения к
закрытому ключу, а значит — вставленного носителя и, как правило, ввода пароля
к контейнеру. Дёргать его на каждый запрос нельзя.

Вход двухступенчатый, и это следствие машиночитаемой доверенности. Один
сертификат представителя действует за несколько организаций, поэтому сервер,
проверив подпись, отвечает не токеном, а списком: под кем именно входим. ИНН
нужно передать явно — без него ответ прямой: «Невозможно однозначно определить
под какой организацией выполняется авторизация».

Список организаций отдаёт ГИС МТ (`/auth/cert/`), а токен — True API
(`/auth/simpleSignIn`). Строка для подписи у каждой системы своя, поэтому
выяснение состава и вход — два разных обращения к ключу.

Срок годности токена сервер не сообщает, поэтому он не зашит и здесь: токен
обновляется по собственному запасу и по ответу 401 — один прозрачный повтор
входа. При таком устройстве точное значение перестаёт что-либо значить.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from . import crypto, transport
from .models import Contour

# Сколько считать токен годным, если сервер не сказал иного. Лишний вход стоит
# одного обращения к ключу, а протухший посреди приёмки — прерванной работы.
DEFAULT_LIFETIME = timedelta(minutes=30)

# За сколько до конца срока обновлять, не дожидаясь отказа.
RENEW_MARGIN = timedelta(minutes=2)


# Как система сообщает, что не может выбрать организацию за нас. Тексты
# приведены дословно, вместе с опечаткой сервера: боевой контур отвечает
# «Невозможно однозначно определить под какой организацией выполняется
# авторизация. Укажите запросе INN». Различать такой отказ приходится по
# сообщению: код ответа у него обычный четырёхсотый, как у всех прочих.
ORGANISATION_MARKS: tuple[str, ...] = (
    "несколько организаций",
    "однозначно определить",
    "запросе inn",
    "укажите инн",
)


def needs_organisation(error: object) -> bool:
    """Отказ ли это из-за того, что не выбрана организация."""
    text = str(error).lower()
    return any(mark in text for mark in ORGANISATION_MARKS)


@dataclass(frozen=True, slots=True)
class Organisation:
    """Участник оборота, за которого действует сертификат."""

    inn: str = ""
    name: str = ""

    @property
    def title(self) -> str:
        return f"{self.name} · ИНН {self.inn}" if self.name else self.inn


@dataclass
class Session:
    """Кто вошёл, каким сертификатом, за какую организацию и до какого времени."""

    contour: Contour = Contour.SANDBOX
    token: str = ""
    thumbprint: str = ""
    owner: str = ""
    inn: str = ""
    organisation: str = ""
    expires_at: datetime | None = None
    # Общая на процесс: под ней идёт обновление токена, чтобы две фоновые
    # задачи не дёрнули закрытый ключ одновременно.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def active(self) -> bool:
        return bool(self.token)

    @property
    def expiring(self) -> bool:
        if not self.expires_at:
            return False
        return datetime.now() >= self.expires_at - RENEW_MARGIN

    @property
    def title(self) -> str:
        where = "" if self.contour is Contour.PRODUCTION else " · песочница"
        return f"{self.organisation or self.owner}{where}"

    def clear(self) -> None:
        self.token = ""
        self.expires_at = None

    def forget(self) -> None:
        """Полный выход: забывается и сертификат, и организация."""
        self.clear()
        self.thumbprint = ""
        self.owner = ""
        self.inn = ""
        self.organisation = ""


# Одна сессия на процесс: вкладка маркировки и фоновые задачи смотрят на неё же.
session = Session()


def organisations(thumbprint: str,
                  contour: Contour = Contour.SANDBOX) -> list[Organisation]:
    """За кого может действовать этот сертификат.

    Спрашивается у ГИС МТ: True API без ИНН отвечает отказом, а не списком.
    Требует обращения к закрытому ключу — сертификат надо предъявить, чтобы
    узнать его полномочия.
    """
    challenge = transport.get("ismp", "/auth/cert/key", contour=contour)
    uuid_value, data = parse_challenge(challenge)
    signature = crypto.sign(data, thumbprint)
    answer = transport.post("ismp", "/auth/cert/", contour=contour,
                            body={"uuid": uuid_value, "data": signature})
    if not isinstance(answer, dict):
        return []
    found = []
    for item in answer.get("organisations") or []:
        if isinstance(item, dict):
            found.append(Organisation(inn=str(item.get("inn") or ""),
                                      name=str(item.get("fullName") or "")))
    return found


def sign_in(thumbprint: str, inn: str = "",
            contour: Contour = Contour.SANDBOX,
            organisation: str = "") -> Session:
    """Вход по сертификату: случайная строка → подпись → токен.

    `inn` обязателен, когда сертификат действует за несколько организаций.
    Отсутствие ИНН при этом — не наша догадка: сервер отвечает об этом прямо, и
    его текст попадает пользователю без пересказа.
    """
    certificate = crypto.find(thumbprint)
    if certificate is None:
        raise crypto.SigningFailed(
            "Сертификат не найден. Возможно, подключён другой носитель ключа.")

    challenge = transport.get("trueapi", "/auth/key", contour=contour)
    uuid_value, data = parse_challenge(challenge)
    signature = crypto.sign(data, thumbprint)

    body: dict[str, Any] = {"uuid": uuid_value, "data": signature}
    if inn.strip():
        body["inn"] = inn.strip()
    granted = transport.post("trueapi", "/auth/simpleSignIn", contour=contour,
                             body=body)

    session.contour = contour
    session.token = _token(granted)
    session.thumbprint = certificate.thumbprint
    session.owner = certificate.owner
    session.inn = inn.strip() or certificate.inn
    # Название организации приходит из списка, в котором её выбрали. При
    # обновлении токена оно не передаётся — и тогда сохраняется прежнее, а не
    # затирается пустотой.
    session.organisation = organisation.strip() or session.organisation
    session.expires_at = _expiry(granted)
    return session


def parse_challenge(answer: Any) -> tuple[str, str]:
    """Разбирает ответ на запрос строки для подписи.

    Общий для всех трёх систем: строку для подписи одинаково выдают и ГИС МТ,
    и True API, и СУЗ, — а разбирать её в трёх местах значит трижды написать
    одну и ту же проверку и один раз ошибиться.
    """
    if not isinstance(answer, dict):
        raise transport.MarkingError(
            "Система маркировки вернула неожиданный ответ на запрос строки "
            "для подписи.")
    uuid_value = str(answer.get("uuid") or "")
    data = str(answer.get("data") or "")
    if not uuid_value or not data:
        raise transport.MarkingError(
            "В ответе нет строки для подписи — вход невозможен.")
    return uuid_value, data


def _token(answer: Any) -> str:
    if not isinstance(answer, dict):
        raise transport.MarkingError(
            "Система маркировки вернула неожиданный ответ на запрос токена.")
    token = str(answer.get("token") or "")
    if token:
        return token
    if answer.get("organisations"):
        # Подпись принята, но не сказано, за кого входим. Список организаций
        # уже здесь — интерфейсу остаётся дать выбрать.
        names = ", ".join(
            str(item.get("fullName") or item.get("inn") or "")
            for item in answer["organisations"] if isinstance(item, dict))
        raise transport.MarkingError(
            "Сертификат действует за несколько организаций — укажите, под "
            f"какой входить: {names}")
    raise transport.AuthRequired(
        "Подпись принята, но токен не выдан. Проверьте, что сертификат "
        "зарегистрирован в личном кабинете этого контура.", 401)


def _expiry(answer: Any) -> datetime:
    """Срок годности токена — из ответа, если он там есть.

    Имя поля у разных методов разное, а чаще его нет вовсе; тогда берётся свой
    запас. `expireDate` приходит меткой времени в миллисекундах.
    """
    now = datetime.now()
    if isinstance(answer, dict):
        for name in ("expires_in", "expiresIn", "ttl", "lifetime"):
            value = answer.get(name)
            if isinstance(value, (int, float)) and value > 0:
                return now + timedelta(seconds=float(value))
        stamp = answer.get("expireDate")
        if isinstance(stamp, (int, float)) and stamp > 0:
            try:
                moment = datetime.fromtimestamp(float(stamp) / 1000)
            except (OverflowError, OSError, ValueError):
                return now + DEFAULT_LIFETIME
            if moment > now:
                return moment
    return now + DEFAULT_LIFETIME


def token() -> str:
    """Действующий токен, при необходимости обновлённый.

    Обновление под блокировкой: без неё две фоновые задачи, столкнувшись с
    истёкшим токеном одновременно, дважды дёрнут закрытый ключ и покажут
    пользователю два окна ввода пароля подряд.
    """
    with session.lock:
        if not session.thumbprint:
            raise transport.AuthRequired(
                "Вход в систему маркировки не выполнен. Выберите сертификат "
                "на вкладке «Маркировка».")
        if session.active and not session.expiring:
            return session.token
        sign_in(session.thumbprint, session.inn, session.contour)
        return session.token


def authorized(call: Callable[[str], Any]) -> Any:
    """Выполняет запрос с токеном, переживая его протухание.

    Один прозрачный повтор: если сервер ответил 401, токен сбрасывается и вход
    выполняется заново. Повтор ровно один — если и он получил отказ, дело не в
    сроке, а в правах, и крутить цикл незачем.

    На 401 сервер запрос не исполнил, поэтому повтор не создаёт второй операции.
    """
    try:
        return call(token())
    except transport.AuthRequired:
        session.clear()
        return call(token())


def sign_out() -> None:
    session.forget()
