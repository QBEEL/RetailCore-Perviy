"""Учётные записи. Всё, кроме «кто я», доступно только администратору."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status

from .. import auditlog, db, security
from ..schemas import UserIn, UserOut
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


def _save_directions(user_id: int, codes: list[str]) -> None:
    """Переписывает направления учётки.

    Целиком, а не по одному: список короткий, приходит из диалога сразу
    готовым, и разбирать, что добавилось, а что убралось, здесь не за чем.
    """
    db.execute("DELETE FROM user_direction WHERE user_id = %s", (user_id,))
    for code in codes:
        # Направления расхода учётке не назначаются: пересчёт закреплений
        # разнёс бы «Налоги» по всем поставщикам человека. Молча, а не
        # отказом: приложение 3.6.3 покажет их флажками в учётке, и отказ
        # сохранить всю учётку из-за одной лишней галочки был бы непонятен.
        db.execute(
            "INSERT INTO user_direction (user_id, direction_id)"
            " SELECT %s, id FROM direction WHERE code = %s AND for_people"
            " ON CONFLICT DO NOTHING", (user_id, code))


def _save_pages(user_id: int, codes: list[str]) -> None:
    """Переписывает список закрытых разделов — так же целиком, как направления."""
    db.execute("DELETE FROM user_page_denied WHERE user_id = %s", (user_id,))
    for code in codes:
        db.execute(
            "INSERT INTO user_page_denied (user_id, page_code) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING", (user_id, code))


def _save_ai_limit(user_id: int, form: UserIn) -> None:
    """Личный предел ИИ-правки. Не переданный клиентом — остаётся как был."""
    if "ai_limit" not in form.model_fields_set:
        return
    db.execute("UPDATE app_user SET ai_daily_limit = %s WHERE id = %s",
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
    _save_directions(row["id"], form.directions)
    _save_pages(row["id"], form.denied_pages)
    _save_ai_limit(row["id"], form)
    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action)"
        " VALUES (%s, 'app_user', %s, 'create')", (user.id, row["id"]))
    # Пароль возвращается один раз, в ответ на создание: в базе лежит только
    # хеш, и восстановить его потом нельзя — можно лишь назначить новый.
    return {"id": row["id"], "login": login, "password": password}


@router.put("/{user_id}", response_model=UserOut, summary="Изменить учётку")
def update_user(user_id: int, form: UserIn,
                user: User = Depends(security.admin_only)) -> UserOut:
    old = db.fetch_one("SELECT ai_daily_limit FROM app_user WHERE id = %s", (user_id,))
    if not old:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Учётная запись не найдена")
    if user_id == user.id and not form.is_admin:
        # Иначе единственный администратор способен разжаловать сам себя, и
        # заводить учётки станет некому.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Нельзя снять с себя права администратора")

    db.execute(
        "UPDATE app_user SET full_name = %s, is_admin = %s, is_active = %s,"
        # Отключение заканчивает выданные входы: включённая позже учётка
        # входит заново. Правая часть видит строку до изменения.
        "                    token_version = token_version"
        "                      + CASE WHEN is_active AND NOT %s THEN 1 ELSE 0 END"
        " WHERE id = %s",
        (form.full_name, form.is_admin, form.is_active, form.is_active, user_id))
    db.execute("DELETE FROM user_responsible WHERE user_id = %s", (user_id,))
    for name in form.responsible:
        db.execute("INSERT INTO user_responsible (user_id, responsible)"
                   " VALUES (%s, %s) ON CONFLICT DO NOTHING", (user_id, name))
    _save_directions(user_id, form.directions)
    _save_pages(user_id, form.denied_pages)
    old_limit = old["ai_daily_limit"]
    _save_ai_limit(user_id, form)
    changes = {}
    if "ai_limit" in form.model_fields_set and form.ai_limit != old_limit:
        # Смена предела — решение администратора о расходах: в журнале видно, кто
        # и с какого значения на какое.
        changes = {"ai_limit": form.ai_limit, auditlog.WAS: {"ai_limit": old_limit}}
    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action, changes)"
        " VALUES (%s, 'app_user', %s, 'update', %s)",
        (user.id, user_id, json.dumps(changes, ensure_ascii=False)))
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
