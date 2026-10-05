"""Вход, обновление токена и смена пароля."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from .. import db, security
from ..schemas import PasswordChange, Token
from ..security import User
from ..settings import settings

router = APIRouter(prefix="/api/auth", tags=["Вход"])


def _token_for(row: dict, auth_time: int | None = None) -> Token:
    names = db.fetch_all(
        "SELECT responsible FROM user_responsible WHERE user_id = %s ORDER BY 1",
        (row["id"],))
    own = db.fetch_all(
        "SELECT d.code FROM user_direction ud JOIN direction d ON d.id = ud.direction_id"
        " WHERE ud.user_id = %s ORDER BY d.sort_order", (row["id"],))
    # Закрытые разделы едут вместе с входом: приложение строит меню до первого
    # запроса к данным, и отдельный поход за правами задержал бы запуск.
    denied = db.fetch_all(
        "SELECT page_code FROM user_page_denied WHERE user_id = %s ORDER BY 1",
        (row["id"],))
    return Token(
        access_token=security.create_token(
            row["id"], row["login"], row.get("token_version", 0), auth_time),
        expires_in=settings.token_hours * 3600,
        login=row["login"],
        user_id=row["id"],
        full_name=row["full_name"],
        is_admin=row["is_admin"],
        responsible=[n["responsible"] for n in names],
        directions=[d["code"] for d in own],
        denied_pages=[p["page_code"] for p in denied],
        must_change_password=bool(row.get("must_change_password", False)),
    )


@router.post("/token", response_model=Token, summary="Войти по логину и паролю")
def login(form: OAuth2PasswordRequestForm = Depends()) -> Token:
    try:
        row = security.authenticate(form.username, form.password)
    except security.TooManyAttempts as blocked:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail=str(blocked)) from None
    if not row:
        # Один и тот же ответ на неверный логин и на неверный пароль: иначе
        # по нему можно узнать, какие учётки существуют.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Неверный логин или пароль",
                            headers={"WWW-Authenticate": "Bearer"})
    return _token_for(row)


@router.post("/refresh", response_model=Token, summary="Продлить токен")
def refresh(user: User = Depends(security.current_user)) -> Token:
    # Продлевать можно ограниченное время после входа по паролю: дальше
    # пароль спрашивается заново.
    if time.time() - user.auth_time > settings.session_days * 86400:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Сессия закончилась — войдите заново",
                            headers={"WWW-Authenticate": "Bearer"})
    row = db.fetch_one(
        "SELECT id, login, full_name, is_admin, must_change_password,"
        "       token_version"
        " FROM app_user WHERE id = %s", (user.id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Учётная запись не найдена")
    # Момент входа переносится из прежнего токена, а не начинается заново.
    return _token_for(row, user.auth_time)


@router.post("/password", response_model=Token,
             summary="Сменить свой пароль")
def change_password(form: PasswordChange,
                    user: User = Depends(security.current_user)) -> Token:
    row = db.fetch_one("SELECT password_hash FROM app_user WHERE id = %s",
                       (user.id,))
    if not row or not security.verify_password(form.old_password,
                                               row["password_hash"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Текущий пароль указан неверно")
    if form.new_password == form.old_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Новый пароль должен отличаться от прежнего")
    # Версия растёт: входы, выданные до смены пароля, заканчиваются, в том
    # числе нынешний, поэтому новый токен возвращается в ответе, и клиент
    # подхватывает его сам.
    changed = db.fetch_one(
        "UPDATE app_user SET password_hash = %s, must_change_password = FALSE,"
        "                    token_version = token_version + 1"
        " WHERE id = %s"
        " RETURNING id, login, full_name, is_admin, must_change_password,"
        "           token_version",
        (security.hash_password(form.new_password), user.id))
    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action)"
        " VALUES (%s, 'app_user', %s, 'password')", (user.id, user.id))
    return _token_for(changed)
