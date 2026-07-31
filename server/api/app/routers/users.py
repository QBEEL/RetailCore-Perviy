"""Учётные записи. Всё, кроме «кто я», доступно только администратору."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from .. import db, security
from ..schemas import UserIn, UserOut
from ..security import User

router = APIRouter(prefix="/api/users", tags=["Пользователи"])

_SELECT = (
    "SELECT u.id, u.login, u.full_name, u.is_admin, u.is_active, u.created_at,"
    "       COALESCE(array_agg(r.responsible)"
    "                FILTER (WHERE r.responsible IS NOT NULL), '{}')"
    "         AS responsible"
    " FROM app_user u LEFT JOIN user_responsible r ON r.user_id = u.id"
)


@router.get("/me", response_model=UserOut, summary="Кто я")
def me(user: User = Depends(security.current_user)) -> UserOut:
    row = db.fetch_one(_SELECT + " WHERE u.id = %s GROUP BY u.id", (user.id,))
    return UserOut(**row)


@router.get("", response_model=list[UserOut], summary="Все учётные записи")
def list_users(user: User = Depends(security.admin_only)) -> list[UserOut]:
    return [UserOut(**row) for row in db.fetch_all(
        _SELECT + " GROUP BY u.id ORDER BY u.full_name")]


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED,
             summary="Завести учётную запись")
def create_user(form: UserIn,
                user: User = Depends(security.admin_only)) -> dict:
    login = form.login.strip().lower()
    if db.fetch_one("SELECT 1 FROM app_user WHERE login = %s", (login,)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Логин {login} уже занят")

    password = security.random_password()
    row = db.fetch_one(
        "INSERT INTO app_user (login, full_name, password_hash, is_admin,"
        "                      is_active, must_change_password)"
        " VALUES (%s, %s, %s, %s, %s, TRUE) RETURNING id",
        (login, form.full_name, security.hash_password(password),
         form.is_admin, form.is_active))
    for name in form.responsible:
        db.execute("INSERT INTO user_responsible (user_id, responsible)"
                   " VALUES (%s, %s) ON CONFLICT DO NOTHING", (row["id"], name))
    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action)"
        " VALUES (%s, 'app_user', %s, 'create')", (user.id, row["id"]))
    # Пароль возвращается один раз, в ответ на создание: в базе лежит только
    # хеш, и восстановить его потом нельзя — можно лишь назначить новый.
    return {"id": row["id"], "login": login, "password": password}


@router.put("/{user_id}", response_model=UserOut, summary="Изменить учётку")
def update_user(user_id: int, form: UserIn,
                user: User = Depends(security.admin_only)) -> UserOut:
    if not db.fetch_one("SELECT 1 FROM app_user WHERE id = %s", (user_id,)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Учётная запись не найдена")
    if user_id == user.id and not form.is_admin:
        # Иначе единственный администратор способен разжаловать сам себя, и
        # заводить учётки станет некому.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Нельзя снять с себя права администратора")

    db.execute(
        "UPDATE app_user SET full_name = %s, is_admin = %s, is_active = %s"
        " WHERE id = %s",
        (form.full_name, form.is_admin, form.is_active, user_id))
    db.execute("DELETE FROM user_responsible WHERE user_id = %s", (user_id,))
    for name in form.responsible:
        db.execute("INSERT INTO user_responsible (user_id, responsible)"
                   " VALUES (%s, %s) ON CONFLICT DO NOTHING", (user_id, name))
    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action)"
        " VALUES (%s, 'app_user', %s, 'update')", (user.id, user_id))
    return UserOut(**db.fetch_one(_SELECT + " WHERE u.id = %s GROUP BY u.id",
                                  (user_id,)))


@router.post("/{user_id}/password", response_model=dict,
             summary="Назначить новый пароль")
def reset_password(user_id: int,
                   user: User = Depends(security.admin_only)) -> dict:
    password = security.random_password()
    # Назначенный администратором пароль владельцу учётки придётся заменить:
    # его видел не только он.
    changed = db.execute(
        "UPDATE app_user SET password_hash = %s, must_change_password = TRUE"
        " WHERE id = %s", (security.hash_password(password), user_id))
    if not changed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Учётная запись не найдена")
    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action)"
        " VALUES (%s, 'app_user', %s, 'reset_password')", (user.id, user_id))
    return {"password": password}
