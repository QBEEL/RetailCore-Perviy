"""Связь с сервером оплат: адрес, токен, запросы, ошибки.

Модуль не зависит от Qt: запрос уходит из фоновой задачи, а разбирать ответ и
показывать ошибку должен уметь и pytest без запущенного интерфейса.

Используется `urllib` из стандартной библиотеки, а не `requests`: приложение
собирается в один exe, и каждая новая зависимость — это лишние мегабайты и ещё
одна библиотека, которую придётся обновлять при находке уязвимости. Запросов
здесь десяток видов, ради них тянуть внешний клиент незачем.

Токен живёт в памяти процесса и на диск не пишется. Пароль тем более: при
следующем запуске приложение спросит его заново.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

TIMEOUT = 120
# Сервер отдаёт всю историю одним ответом — несколько мегабайт JSON. На
# офисном канале это секунды, поэтому таймаут заметно больше обычного.


class ServerError(Exception):
    """Сервер ответил, но отказал. Текст пригоден для показа пользователю."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class AuthError(ServerError):
    """Нужен вход: токена нет, он истёк или учётку отключили."""


class OfflineError(ServerError):
    """До сервера не достучались. Данные не изменились — можно повторить."""


@dataclass
class Session:
    """Кто вошёл и чем подписаны запросы."""

    base_url: str = ""
    token: str = ""
    login: str = ""
    full_name: str = ""
    is_admin: bool = False
    responsible: tuple[str, ...] = ()
    expires_at: datetime | None = None
    # Пароль выдан администратором: работать можно, но приложение потребует
    # заменить его прежде, чем показать данные.
    must_change_password: bool = False

    @property
    def active(self) -> bool:
        return bool(self.token and self.base_url)

    @property
    def expiring(self) -> bool:
        """Пора продлевать: до конца срока меньше часа."""
        if not self.expires_at:
            return False
        return datetime.now() >= self.expires_at - timedelta(hours=1)

    def may_edit(self, responsible: str) -> bool:
        return self.is_admin or responsible in self.responsible

    def clear(self) -> None:
        self.token = ""
        self.login = ""
        self.full_name = ""
        self.is_admin = False
        self.responsible = ()
        self.expires_at = None
        self.must_change_password = False


# Одна сессия на процесс: окно оплат, фоновые задачи и диалоги смотрят на неё же.
session = Session()


def _url(path: str, params: dict[str, Any] | None = None) -> str:
    url = session.base_url.rstrip("/") + path
    if not params:
        return url
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
    return f"{url}?{urllib.parse.urlencode(pairs)}" if pairs else url


def _send(request: urllib.request.Request) -> Any:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        raise _failure(error) from None
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as error:
        raise OfflineError(
            "Сервер оплат недоступен. Проверьте подключение к сети."
            f"\n\nПодробности: {error}") from None


def _failure(error: urllib.error.HTTPError) -> ServerError:
    """Ответ об ошибке → исключение с человеческим текстом."""
    try:
        detail = json.loads(error.read()).get("detail", "")
    except (ValueError, AttributeError):
        detail = ""
    if isinstance(detail, list):
        # Ошибка проверки полей от FastAPI приходит списком.
        detail = "; ".join(str(item.get("msg", item)) for item in detail)
    if error.code in (401, 403) and not detail:
        detail = "Недостаточно прав"
    if error.code == 401:
        session.clear()
        return AuthError(detail or "Требуется вход", 401)
    return ServerError(detail or f"Сервер ответил кодом {error.code}", error.code)


def _request(method: str, path: str, *, params: dict | None = None,
             body: Any = None, authorized: bool = True) -> Any:
    data, headers = None, {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, default=_encode).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if authorized:
        if not session.active:
            raise AuthError("Вход в систему не выполнен")
        headers["Authorization"] = f"Bearer {session.token}"
    request = urllib.request.Request(_url(path, params), data=data,
                                     headers=headers, method=method)
    return _send(request)


def _encode(value: Any) -> str:
    """Даты и время в JSON — строкой ISO, как их ждёт сервер."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} не сериализуется")


def get(path: str, params: dict | None = None) -> Any:
    return _request("GET", path, params=params)


def post(path: str, body: Any = None, params: dict | None = None) -> Any:
    return _request("POST", path, body=body, params=params)


def patch(path: str, body: Any = None) -> Any:
    return _request("PATCH", path, body=body)


def put(path: str, body: Any = None) -> Any:
    return _request("PUT", path, body=body)


def delete(path: str) -> Any:
    return _request("DELETE", path)


# --- файлы ---------------------------------------------------------------------

def upload(path: str, source: str) -> Any:
    """Отправляет файл на сервер как multipart/form-data.

    Тело собирается вручную: в стандартной библиотеке готового кодировщика
    multipart нет, а тянуть ради одной загрузки внешний клиент незачем. Файл
    читается целиком — вложения ограничены сервером двадцатью пятью мегабайтами.
    """
    if not session.active:
        raise AuthError("Вход в систему не выполнен")
    boundary = "----RetailCore" + uuid.uuid4().hex
    name = os.path.basename(source)
    with open(source, "rb") as handle:
        content = handle.read()

    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")

    request = urllib.request.Request(
        _url(path), data=head + content + tail,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {session.token}",
            "Accept": "application/json",
        }, method="POST")
    return _send(request)


def download(path: str, target: str) -> str:
    """Скачивает вложение в указанный файл."""
    if not session.active:
        raise AuthError("Вход в систему не выполнен")
    request = urllib.request.Request(
        _url(path), headers={"Authorization": f"Bearer {session.token}"})
    partial = target + ".part"
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            with open(partial, "wb") as handle:
                while chunk := response.read(1024 * 256):
                    handle.write(chunk)
    except urllib.error.HTTPError as error:
        _drop(partial)
        raise _failure(error) from None
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as error:
        _drop(partial)
        raise OfflineError(f"Не удалось скачать вложение: {error}") from None
    # Переименование последним шагом: оборванная закачка не должна остаться
    # под именем целого файла и открыться потом как испорченный документ.
    os.replace(partial, target)
    return target


def _drop(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --- вход ----------------------------------------------------------------------

def sign_in(base_url: str, login: str, password: str) -> Session:
    """Вход по логину и паролю. Заполняет общую сессию процесса."""
    session.base_url = base_url.rstrip("/")
    body = urllib.parse.urlencode({"username": login, "password": password})
    request = urllib.request.Request(
        _url("/api/auth/token"), data=body.encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    answer = _send(request)
    _adopt(answer)
    return session


def refresh() -> Session:
    """Продлевает токен, пока старый ещё действует."""
    _adopt(post("/api/auth/refresh"))
    return session


def _adopt(answer: dict) -> None:
    session.token = answer["access_token"]
    session.login = answer["login"]
    session.full_name = answer["full_name"]
    session.is_admin = bool(answer["is_admin"])
    session.responsible = tuple(answer.get("responsible", ()))
    session.must_change_password = bool(answer.get("must_change_password", False))
    session.expires_at = datetime.now() + timedelta(
        seconds=int(answer.get("expires_in", 0)))


def sign_out() -> None:
    session.clear()


def change_password(old_password: str, new_password: str) -> None:
    post("/api/auth/password",
         {"old_password": old_password, "new_password": new_password})
