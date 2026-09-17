"""Сохранённый вход: токен между запусками и льготный срок без сервера.

Приложение требует входа, но требовать пароль на каждый запуск — значит
заставлять вводить его по десять раз на дню: программу закрывают и открывают
вместе с очередным файлом. Поэтому здесь хранится токен, а не пароль.

Пароль не сохраняется никогда и ни в каком виде. Токен живёт двенадцать часов
и на сервере отзывается отключением учётной записи — украденный файл профиля
даёт доступ до конца дня, а не навсегда.

Второе назначение — льготный срок. Сервер может лечь, а работа с файлами от
него не зависит: сопоставление, заказ и переоценка читают Excel и локальные
базы. Запрещать их из-за недоступного сервера — значит останавливать отдел
из-за чужой аварии. Поэтому запоминается дата последнего удачного входа, и
неделю после неё программа работает без сети, закрыв только то, что без
сервера всё равно не работает.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import appdata

FILE_NAME = "session.json"

# Сколько дней можно работать без связи с сервером после последнего входа.
# Неделя закрывает отпуск на выходных и аварию, растянувшуюся на несколько
# дней, но не превращает офлайн в постоянный режим работы.
GRACE_DAYS = 7


@dataclass(slots=True)
class Saved:
    """Что известно о прошлом входе."""

    base_url: str = ""
    login: str = ""
    user_id: int = 0
    full_name: str = ""
    is_admin: bool = False
    responsible: tuple[str, ...] = ()
    directions: tuple[str, ...] = ()
    # Разделы, закрытые администратором. Хранятся вместе с остальным входом:
    # иначе неделя работы без сервера возвращала бы человеку вкладки, которые
    # ему закрыли, — а льготный срок для того и есть, чтобы всё было как обычно.
    denied_pages: tuple[str, ...] = ()
    token: str = ""
    expires_at: datetime | None = None
    # Когда сервер в последний раз подтвердил, кто мы. Отсюда считается
    # льготный срок, и обновляется она при каждом удачном входе.
    confirmed_at: datetime | None = None

    @property
    def valid(self) -> bool:
        """Токен ещё жив — входить заново не нужно."""
        if not (self.token and self.base_url and self.expires_at):
            return False
        # Запас в минуту: токен, истекающий на следующем вздохе, лучше считать
        # истёкшим здесь, чем получить 401 на первом же запросе.
        return datetime.now() < self.expires_at - timedelta(minutes=1)

    @property
    def known(self) -> bool:
        """Человек хоть раз входил на этой машине."""
        return bool(self.login and self.confirmed_at)

    def grace_left(self) -> int:
        """Сколько дней осталось работать без сервера. Ноль — срок вышел."""
        if not self.confirmed_at:
            return 0
        spent = (datetime.now() - self.confirmed_at).days
        return max(0, GRACE_DAYS - spent)


def path() -> str:
    return appdata.path_to(FILE_NAME)


def load() -> Saved:
    """Читает сохранённый вход. Испорченный файл — то же, что его отсутствие."""
    try:
        with open(path(), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return Saved()
    return Saved(
        base_url=str(raw.get("base_url", "")),
        login=str(raw.get("login", "")),
        user_id=int(raw.get("user_id", 0) or 0),
        full_name=str(raw.get("full_name", "")),
        is_admin=bool(raw.get("is_admin", False)),
        responsible=tuple(raw.get("responsible", ())),
        directions=tuple(raw.get("directions", ())),
        denied_pages=tuple(raw.get("denied_pages", ())),
        token=str(raw.get("token", "")),
        expires_at=_moment(raw.get("expires_at")),
        confirmed_at=_moment(raw.get("confirmed_at")),
    )


def save(session: object) -> None:
    """Запоминает текущий вход. Сбой записи не должен мешать работе."""
    saved = Saved(
        base_url=getattr(session, "base_url", ""),
        login=getattr(session, "login", ""),
        user_id=int(getattr(session, "user_id", 0) or 0),
        full_name=getattr(session, "full_name", ""),
        is_admin=bool(getattr(session, "is_admin", False)),
        responsible=tuple(getattr(session, "responsible", ())),
        directions=tuple(getattr(session, "directions", ())),
        denied_pages=tuple(getattr(session, "denied_pages", ())),
        token=getattr(session, "token", ""),
        expires_at=getattr(session, "expires_at", None),
        confirmed_at=datetime.now(),
    )
    _write(saved)


def forget() -> None:
    """Выход из учётной записи: токен удаляется, память о входе остаётся.

    Логин и дата последнего подтверждения переживают выход намеренно — по ним
    подставляется имя в окне входа, а льготный срок не должен обнуляться
    оттого, что человек вышел сам.
    """
    saved = load()
    saved.token = ""
    saved.expires_at = None
    _write(saved)


def _write(saved: Saved) -> None:
    payload = {
        "base_url": saved.base_url,
        "login": saved.login,
        "user_id": saved.user_id,
        "full_name": saved.full_name,
        "is_admin": saved.is_admin,
        "responsible": list(saved.responsible),
        "directions": list(saved.directions),
        "denied_pages": list(saved.denied_pages),
        "token": saved.token,
        "expires_at": saved.expires_at.isoformat() if saved.expires_at else "",
        "confirmed_at": saved.confirmed_at.isoformat() if saved.confirmed_at else "",
    }
    target = path()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        # Файл содержит действующий токен: читать его посторонним на общей
        # машине незачем. На Windows это ограничение приблизительное, но
        # лучше, чем ничего, и ошибку прав игнорируем — не ради неё всё.
        os.chmod(target, 0o600)
    except OSError:
        pass


def _moment(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
