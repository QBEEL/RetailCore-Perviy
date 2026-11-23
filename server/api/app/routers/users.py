"""Учётные записи. Всё, кроме «кто я», доступно только администратору."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import errors

from .. import auditlog, db, security
from ..schemas import UserIn, UserOut, UserUpdate
from ..security import User

router = APIRouter(prefix="/api/users", tags=["Пользователи"])

_SELECT = (
    "SELECT u.id, u.login, u.full_name, u.is_admin, u.is_active, u.created_at,"
    "       COALESCE(array_agg(r.responsible)"
    "                FILTER (WHERE r.responsible IS NOT NULL), '{}')"
    "         AS responsible,"
    # Направления — признак категорийного менеджера. Отдельного поля
    # «должность» нет: поставщиков ведут ровно те, у кого задано направление,
    # и второй признак того же самого рано или поздно разошёлся бы с первым.
    "       COALESCE((SELECT array_agg(d.code ORDER BY d.sort_order)"
    "                   FROM user_direction ud"
    "                   JOIN direction d ON d.id = ud.direction_id"
    "                  WHERE ud.user_id = u.id), '{}') AS directions,"
    # Закрытые разделы приложения. Подзапросом, как и направления: с ещё одним
    # LEFT JOIN строки размножились бы и array_agg по именам из 1С считал бы
    # каждое столько раз, сколько у человека закрыто вкладок.
    "       COALESCE((SELECT array_agg(p.page_code ORDER BY p.page_code)"
    "                   FROM user_page_denied p"
    "                  WHERE p.user_id = u.id), '{}') AS denied_pages,"
    "       u.ai_daily_limit AS ai_limit,"
    "       COALESCE(u.app_version, '') AS app_version,"
    "       COALESCE(u.app_platform, '') AS app_platform, u.app_seen_at,"
    "       COALESCE((SELECT a.requests FROM ai_usage a WHERE a.user_id = u.id"
    "                    AND a.day = (now() AT TIME ZONE 'Europe/Moscow')::date), 0)"
    "         AS ai_used"
    " FROM app_user u LEFT JOIN user_responsible r ON r.user_id = u.id"
)


def _save_access(handle, user_id: int, form: UserIn) -> None:
    handle.execute("DELETE FROM user_responsible WHERE user_id = %s", (user_id,))
    for name in form.responsible:
        handle.execute("INSERT INTO user_responsible (user_id, responsible)"
                       " VALUES (%s, %s) ON CONFLICT DO NOTHING", (user_id, name))
    handle.execute("DELETE FROM user_direction WHERE user_id = %s", (user_id,))
    for code in form.directions:
        # Направления расхода учётке не назначаются: пересчёт закреплений
        # разнёс бы «Налоги» по всем поставщикам человека. Молча, а не
        # отказом: приложение 3.6.3 покажет их флажками в учётке, и отказ
        # сохранить всю учётку из-за одной лишней галочки был бы непонятен.
        handle.execute(
            "INSERT INTO user_direction (user_id, direction_id)"
            " SELECT %s, id FROM direction WHERE code = %s AND for_people"
            " ON CONFLICT DO NOTHING", (user_id, code))
    handle.execute("DELETE FROM user_page_denied WHERE user_id = %s", (user_id,))
    for code in form.denied_pages:
        handle.execute(
            "INSERT INTO user_page_denied (user_id, page_code) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING", (user_id, code))
    if "ai_limit" in form.model_fields_set:
        handle.execute("UPDATE app_user SET ai_daily_limit = %s WHERE id = %s",
                       (form.ai_limit, user_id))


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
    password = security.random_password()
    password_hash = security.hash_password(password)
    with db.cursor() as handle:
        handle.execute(
            "INSERT INTO app_user (login, full_name, password_hash, is_admin,"
            "                      is_active, must_change_password)"
            " VALUES (%s, %s, %s, %s, %s, TRUE)"
            " ON CONFLICT (login) DO NOTHING RETURNING id",
            (login, form.full_name, password_hash, form.is_admin, form.is_active))
        row = handle.fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"Логин {login} уже занят")
        _save_access(handle, row["id"], form)
        handle.execute(
            "INSERT INTO audit_log (user_id, entity, entity_id, action)"
            " VALUES (%s, 'app_user', %s, 'create')", (user.id, row["id"]))
    # Пароль возвращается один раз, в ответ на создание: в базе лежит только
    # хеш, и восстановить его потом нельзя — можно лишь назначить новый.
    return {"id": row["id"], "login": login, "password": password}


@router.put("/{user_id}", response_model=UserOut, summary="Изменить учётку")
@router.patch("/{user_id}", response_model=UserOut, summary="Изменить учётку с новым логином")
def update_user(user_id: int, form: UserUpdate,
                user: User = Depends(security.admin_only)) -> UserOut:
    if user_id == user.id and not form.is_admin:
        # Иначе единственный администратор способен разжаловать сам себя, и
        # заводить учётки станет некому.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Нельзя снять с себя права администратора")

    login = None
    if form.new_login is not None:
        login = form.new_login.strip().lower()
        if not login.replace(".", "").replace("-", "").replace("_", "").isalnum():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Логин должен содержать буквы или цифры. "
                                       "Допустимы точка, дефис и подчёркивание.")

    try:
        with db.cursor() as handle:
            handle.execute("SELECT ai_daily_limit FROM app_user WHERE id = %s", (user_id,))
            old = handle.fetchone()
            if not old:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                    detail="Учётная запись не найдена")
            handle.execute(
                "UPDATE app_user SET login = COALESCE(%s, login), full_name = %s,"
                "                    is_admin = %s, is_active = %s,"
                # Отключение завершает выданные входы.
                "                    token_version = token_version"
                "                      + CASE WHEN is_active AND NOT %s THEN 1 ELSE 0 END"
                " WHERE id = %s AND (NOT %s OR login = %s)",
                (login, form.full_name, form.is_admin, form.is_active, form.is_active,
                 user_id, login is not None, form.login))
            if not handle.rowcount:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                    detail="Учётная запись изменилась. Обновите список и повторите правку.")
            _save_access(handle, user_id, form)
            changes = {}
            was = {}
            if login is not None and login != form.login:
                changes["login"], was["login"] = login, form.login
            if "ai_limit" in form.model_fields_set and form.ai_limit != old["ai_daily_limit"]:
                changes["ai_limit"], was["ai_limit"] = form.ai_limit, old["ai_daily_limit"]
            if was:
                changes[auditlog.WAS] = was
            handle.execute(
                "INSERT INTO audit_log (user_id, entity, entity_id, action, changes)"
                " VALUES (%s, 'app_user', %s, 'update', %s)",
                (user.id, user_id, json.dumps(changes, ensure_ascii=False)))
            handle.execute(_SELECT + " WHERE u.id = %s GROUP BY u.id", (user_id,))
            return UserOut(**handle.fetchone())
    except errors.UniqueViolation as error:
        if error.diag.constraint_name != "app_user_login_key":
            raise
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"Логин {login} уже занят") from None


@router.post("/{user_id}/password", response_model=dict,
             summary="Назначить новый пароль")
def reset_password(user_id: int,
                   user: User = Depends(security.admin_only)) -> dict:
    password = security.random_password()
    # Назначенный администратором пароль владельцу учётки придётся заменить:
    # его видел не только он.
    changed = db.execute(
        "UPDATE app_user SET password_hash = %s, must_change_password = TRUE,"
        "                    token_version = token_version + 1"
        " WHERE id = %s", (security.hash_password(password), user_id))
    if not changed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Учётная запись не найдена")
    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action)"
        " VALUES (%s, 'app_user', %s, 'reset_password')", (user.id, user_id))
    return {"password": password}
