"""Вход, обновление токена и смена пароля."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from .. import db, security
from ..schemas import PasswordChange, Token
from ..security import User
from ..settings import settings

router = APIRouter(prefix="/api/auth", tags=["Вход"])


def _token_for(row: dict) -> Token:
    names = db.fetch_all(
        "SELECT responsible FROM user_responsible WHERE user_id = %s ORDER BY 1",
        (row["id"],))
    return Token(
        access_token=security.create_token(row["id"], row["login"]),
        expires_in=settings.token_hours * 3600,
        login=row["login"],
        full_name=row["full_name"],
        is_admin=row["is_admin"],
        responsible=[n["responsible"] for n in names],
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
    row = db.fetch_one(
        "SELECT id, login, full_name, is_admin, must_change_password"
        " FROM app_user WHERE id = %s", (user.id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Учётная запись не найдена")
    return _token_for(row)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT,
             summary="Сменить свой пароль")
def change_password(form: PasswordChange,
                    user: User = Depends(security.current_user)) -> None:
    row = db.fetch_one("SELECT password_hash FROM app_user WHERE id = %s",
                       (user.id,))
    if not row or not security.verify_password(form.old_password,
                                               row["password_hash"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Текущий пароль указан неверно")
    if form.new_password == form.old_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Новый пароль должен отличаться от прежнего")
    db.execute(
        "UPDATE app_user SET password_hash = %s, must_change_password = FALSE"
        " WHERE id = %s", (security.hash_password(form.new_password), user.id))
    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action)"
        " VALUES (%s, 'app_user', %s, 'password')", (user.id, user.id))
