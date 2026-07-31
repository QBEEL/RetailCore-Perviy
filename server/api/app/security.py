"""Пароли, токены и права доступа.

Пароль хранится как scrypt-хеш из стандартной библиотеки: отдельной зависимости
не требует, собирается везде и по стойкости не уступает bcrypt. Формат записи —
`scrypt$n$r$p$соль$хеш`, параметры лежат рядом с хешем, поэтому их можно будет
поднять со временем, не ломая уже заведённые пароли.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from . import db
from .settings import settings

# Параметры scrypt. 2^15 подобрано под одноядерный сервер: проверка пароля
# занимает около 60 мс — незаметно при входе и дорого при переборе.
_N, _R, _P = 1 << 15, 8, 1
_KEY_LENGTH = 32

# scrypt требует 128 * n * r байт — при этих параметрах ровно 32 МБ, а столько
# же составляет предел OpenSSL по умолчанию, и вычисление в него не помещается.
# Предел задаётся явно с запасом; на память процесса это не влияет, потому что
# буфер живёт только на время одного хеширования.
_MAX_MEMORY = 64 * 1024 * 1024

scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=_N, r=_R, p=_P, maxmem=_MAX_MEMORY,
                            dklen=_KEY_LENGTH)
    return "$".join(["scrypt", str(_N), str(_R), str(_P),
                     base64.b64encode(salt).decode(),
                     base64.b64encode(digest).decode()])


def verify_password(password: str, stored: str) -> bool:
    try:
        marker, n, r, p, salt, digest = stored.split("$")
        if marker != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode("utf-8"),
                                salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p),
                                maxmem=_MAX_MEMORY, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    # Сравнение постоянного времени: обычное «==» выдаёт длину совпадения
    # разницей во времени ответа.
    return hmac.compare_digest(expected, actual)


def create_token(user_id: int, login: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "login": login,
        "iat": now,
        "exp": now + timedelta(hours=settings.token_hours),
    }
    return jwt.encode(payload, settings.secret, algorithm="HS256")


@dataclass(frozen=True, slots=True)
class User:
    id: int
    login: str
    full_name: str
    is_admin: bool
    # Значения `responsible` из выгрузки 1С, которые считаются «своими».
    responsible: frozenset[str]

    def may_edit(self, responsible: str) -> bool:
        """Администратор правит всё, остальные — только свои оплаты."""
        return self.is_admin or responsible in self.responsible


def current_user(token: str = Depends(scheme)) -> User:
    denied = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Требуется вход",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise denied from None

    row = db.fetch_one(
        "SELECT u.id, u.login, u.full_name, u.is_admin, u.is_active,"
        "       COALESCE(array_agg(r.responsible)"
        "                FILTER (WHERE r.responsible IS NOT NULL), '{}') AS responsible"
        " FROM app_user u"
        " LEFT JOIN user_responsible r ON r.user_id = u.id"
        " WHERE u.id = %s GROUP BY u.id",
        (int(payload.get("sub", 0)),))
    # Учётку могли отключить, пока токен ещё жив: проверяем на каждом запросе,
    # иначе уволенный сотрудник работал бы до конца срока действия токена.
    if not row or not row["is_active"]:
        raise denied
    return User(
        id=row["id"],
        login=row["login"],
        full_name=row["full_name"],
        is_admin=row["is_admin"],
        responsible=frozenset(row["responsible"]),
    )


def admin_only(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Действие доступно только администратору")
    return user


# Защита от подбора. Пять попыток на логин за четверть часа: человек, забывший
# пароль, в них укладывается, а перебор словаря — нет.
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = "15 minutes"


class TooManyAttempts(Exception):
    """Вход придержан после серии неудач."""

    def __init__(self, minutes: int) -> None:
        super().__init__(f"Слишком много попыток входа. "
                         f"Повторите через {minutes} мин.")
        self.minutes = minutes


def _recent_failures(login: str) -> int:
    row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM login_attempt"
        f" WHERE login = %s AND NOT success AND at > now() - interval '{ATTEMPT_WINDOW}'",
        (login,))
    return int(row["n"]) if row else 0


def _record_attempt(login: str, success: bool) -> None:
    db.execute("INSERT INTO login_attempt (login, success) VALUES (%s, %s)",
               (login, success))


def authenticate(login: str, password: str) -> dict | None:
    """Проверяет пару логин-пароль. Ошибка подбора считается по логину."""
    login = login.strip().lower()
    if _recent_failures(login) >= MAX_ATTEMPTS:
        raise TooManyAttempts(15)

    row = db.fetch_one(
        "SELECT id, login, full_name, password_hash, is_admin, is_active,"
        "       must_change_password"
        " FROM app_user WHERE login = %s", (login,))
    if not row or not row["is_active"]:
        # Пароль всё равно проверяется по заглушке: без этого несуществующий
        # логин отвечал бы заметно быстрее существующего, и логины можно было
        # бы перебирать по времени ответа.
        verify_password(password, hash_password("нет такого пользователя"))
        _record_attempt(login, False)
        return None
    if not verify_password(password, row["password_hash"]):
        _record_attempt(login, False)
        return None
    _record_attempt(login, True)
    # Удачный вход обнуляет счётчик: иначе человек, ошибившийся четыре раза и
    # вошедший с пятой, остался бы в одном шаге от блокировки на две недели.
    db.execute("DELETE FROM login_attempt WHERE login = %s AND NOT success",
               (login,))
    return row


def random_password(length: int = 12) -> str:
    """Пароль для новой учётки. Без похожих друг на друга символов."""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
