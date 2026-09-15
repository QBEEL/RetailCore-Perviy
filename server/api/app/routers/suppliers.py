"""Поставщики: направления, закрепление за менеджерами, список для вкладки.

Здесь живёт то, чего раньше не было: закрепление как факт. До сих пор «менеджер
поставщика» вычислялся по оплатам — кто чаще платил, тот и значился ведущим.
Теперь это два разных ответа, и они не путаются: `managers` — кто платил,
`assigned` — кто ведёт.

Закрепление двухшаговое. Менеджер заявляет своих поставщиков сам, администратор
подтверждает; после подтверждения правит только он. Заявленное считается своим
сразу, не дожидаясь подтверждения, — иначе между заявкой и фиксацией человек
остаётся без собственного списка и работать ему нечем. Фиксация меняет не
видимость, а право на правку.

Направление поставщика не ограничивает выдачу. Менеджер Beauty видит поставщиков
Fashion: чаще всего вкладку открывают с вопросом «не помню, чей это поставщик»,
и отбор по направлению — это умолчание фильтра, а не право доступа.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import db, security
from ..schemas import (
    AssignIn,
    AssignmentResult,
    ClaimIn,
    DirectionOut,
    DirectionsIn,
    SupplierEntry,
    SupplierPage,
)
from ..security import User

router = APIRouter(prefix="/api/suppliers", tags=["Поставщики"])

# Сортировки, разрешённые в запросе. Белый список, а не имя колонки из
# параметра: подставлять текст запроса в SQL нельзя, а сортировать по чему
# угодно и не нужно.
#
# Сортировка по менеджеру и направлению идёт по первому значению, а не по
# всему набору: у поставщика их бывает несколько, и «первый по алфавиту»
# — единственный порядок, который читается в колонке так же, как выглядит.
# Незакреплённые уходят в конец через NULLS LAST, а не в начало: список
# открывают ради того, кто ведёт, а не ради пустых строк.
_SORTS = {
    "name": "j.recipient",
    "amount": "j.amount",
    "payments": "j.payments",
    "last_pay": "j.last_pay",
    "manager": "j.lead_manager",
    "direction": "j.lead_direction",
}

# Список поставщиков собирается из четырёх независимых частей, и каждая
# считается один раз на всю выборку.
#
# `base` — union, а не одни оплаты: поставщик, которого менеджер уже ведёт, но
# по которому оплат ещё не было, обязан быть в списке. Иначе закрепить его
# нельзя, а именно с таких поставщиков работа обычно и начинается.
#
# Плательщик сводится к учётной записи через `user_responsible`: в выгрузке 1С
# один человек записан десятком разных написаний, и без сведения он занимал бы
# в фильтре десяток строк.
_LIST_CTE = """
WITH paid AS (
    SELECT recipient_key,
           MAX(recipient)   AS recipient,
           MAX(supplier_id) AS supplier_id,
           COUNT(*)         AS payments,
           SUM(amount)      AS amount,
           MAX(pay_date)    AS last_pay
    FROM payment WHERE recipient <> '' GROUP BY recipient_key
), base AS (
    SELECT recipient_key, recipient, supplier_id, payments, amount, last_pay
    FROM paid
    UNION ALL
    SELECT l.recipient_key, l.recipient, l.supplier_id, 0, 0, NULL
    FROM recipient_link l
    WHERE NOT EXISTS (SELECT 1 FROM paid p WHERE p.recipient_key = l.recipient_key)
), payer AS (
    SELECT p.recipient_key,
           COALESCE(u.full_name, p.responsible) AS payer,
           u.id AS user_id,
           COUNT(*) AS n,
           EXISTS (SELECT 1 FROM supplier_assignment a
                   WHERE a.recipient_key = p.recipient_key AND a.user_id = u.id)
             AS is_assigned
    FROM payment p
    LEFT JOIN LATERAL (
        SELECT au.id, au.full_name
        FROM user_responsible ur JOIN app_user au ON au.id = ur.user_id
        WHERE ur.responsible = p.responsible
        ORDER BY au.id LIMIT 1
    ) u ON TRUE
    WHERE p.responsible <> '' AND p.recipient <> ''
    GROUP BY p.recipient_key, COALESCE(u.full_name, p.responsible), u.id
), payers AS (
    SELECT recipient_key,
           array_agg(payer ORDER BY n DESC, payer) AS managers,
           COALESCE(array_agg(payer ORDER BY n DESC, payer)
                    FILTER (WHERE user_id IS NOT NULL AND NOT is_assigned),
                    '{}') AS unassigned
    FROM payer GROUP BY recipient_key
), assigned AS (
    SELECT a.recipient_key,
           json_agg(json_build_object(
               'user_id', a.user_id,
               'full_name', u.full_name,
               'state', a.state,
               'directions', COALESCE(
                   (SELECT array_agg(d.code ORDER BY d.sort_order)
                    FROM user_direction ud JOIN direction d ON d.id = ud.direction_id
                    WHERE ud.user_id = a.user_id), '{}')
           ) ORDER BY u.full_name) AS people,
           array_agg(a.user_id) AS user_ids,
           array_agg(a.state)   AS states,
           MIN(u.full_name)     AS lead
    FROM supplier_assignment a JOIN app_user u ON u.id = a.user_id
    GROUP BY a.recipient_key
), dirs AS (
    SELECT sd.recipient_key, array_agg(d.code ORDER BY d.sort_order) AS codes,
           MIN(d.sort_order) AS lead
    FROM supplier_direction sd JOIN direction d ON d.id = sd.direction_id
    GROUP BY sd.recipient_key
), j AS (
    SELECT b.*,
           COALESCE(p.managers, '{}')       AS managers,
           COALESCE(p.unassigned, '{}')     AS unassigned_payers,
           COALESCE(a.people, '[]'::json)   AS assigned,
           COALESCE(a.user_ids, '{}')       AS assigned_ids,
           COALESCE(a.states, '{}')         AS assigned_states,
           COALESCE(d.codes, '{}')          AS directions,
           -- Без COALESCE: незакреплённому здесь нужен NULL, иначе пустая
           -- строка встанет по алфавиту выше всех фамилий.
           a.lead                           AS lead_manager,
           d.lead                           AS lead_direction
    FROM base b
    LEFT JOIN payers   p ON p.recipient_key = b.recipient_key
    LEFT JOIN assigned a ON a.recipient_key = b.recipient_key
    LEFT JOIN dirs     d ON d.recipient_key = b.recipient_key
)
"""


def _filters(search: str, direction: str, manager: int, state: str,
             ) -> tuple[str, list[Any]]:
    """Условия отбора. Пустое условие не ограничивает выборку."""
    parts: list[str] = []
    values: list[Any] = []
    if search.strip():
        parts.append("j.recipient ILIKE %s")
        values.append(f"%{search.strip()}%")
    if direction:
        parts.append("%s = ANY(j.directions)")
        values.append(direction)
    if manager:
        # «Мои» — это и черновики, и зафиксированное: заявленное считается
        # своим сразу, иначе до подтверждения списка у человека попросту нет.
        parts.append("%s = ANY(j.assigned_ids)")
        values.append(manager)
    if state == "fixed":
        parts.append("'fixed' = ANY(j.assigned_states)")
    elif state == "draft":
        parts.append("'draft' = ANY(j.assigned_states)")
    elif state == "none":
        parts.append("cardinality(j.assigned_ids) = 0")
    return (" WHERE " + " AND ".join(parts) if parts else ""), values


@router.get("/directions", response_model=list[DirectionOut],
            summary="Справочник направлений")
def directions(user: User = Depends(security.current_user)) -> list[DirectionOut]:
    return [DirectionOut(**row) for row in db.fetch_all(
        "SELECT id, code, title, sort_order, is_active FROM direction"
        " WHERE is_active ORDER BY sort_order, title")]


@router.get("/managers", response_model=list[dict],
            summary="Менеджеры, за которыми что-то закреплено")
def managers(user: User = Depends(security.current_user)) -> list[dict]:
    """Наполнение фильтра по менеджеру.

    Только те, за кем что-то закреплено, а не все учётные записи: список нужен
    для отбора, и человек без единого поставщика сделал бы в нём пустую строку.
    """
    return [{"user_id": row["id"], "full_name": row["full_name"],
             "suppliers": int(row["n"])}
            for row in db.fetch_all(
                "SELECT u.id, u.full_name, COUNT(*) AS n"
                " FROM supplier_assignment a JOIN app_user u ON u.id = a.user_id"
                " GROUP BY u.id, u.full_name ORDER BY u.full_name")]


@router.get("", response_model=SupplierPage, summary="Список поставщиков")
def list_suppliers(
    user: User = Depends(security.current_user),
    search: str = "",
    direction: str = "",
    manager: int = 0,
    state: str = Query("", pattern="^(fixed|draft|none)?$"),
    sort: str = Query("amount",
                      pattern="^(name|amount|payments|last_pay|manager|direction)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
) -> SupplierPage:
    """Страница списка. Отбор и сортировка считаются здесь, а не на клиенте.

    Раньше вкладка забирала всех поставщиков разом и фильтровала у себя. С
    направлениями, закреплением и сортировкой по колонкам на полутора тысячах
    записей это уже неприемлемо: каждая смена фильтра пересобирала бы список
    целиком.
    """
    where, values = _filters(search, direction, manager, state)

    total = db.fetch_one(f"{_LIST_CTE} SELECT COUNT(*) AS n FROM j{where}", values)
    # NULLS LAST во всех случаях: поставщик без оплат не должен занимать
    # верх списка, отсортированного по последней оплате.
    query = (f"{_LIST_CTE} SELECT * FROM j{where}"
             f" ORDER BY {_SORTS[sort]} {order.upper()} NULLS LAST, j.recipient"
             " LIMIT %s OFFSET %s")
    rows = db.fetch_all(query, [*values, page_size, (page - 1) * page_size])

    return SupplierPage(
        items=[_entry(row) for row in rows],
        total=int(total["n"]) if total else 0,
        page=page,
        page_size=page_size,
    )


@router.get("/claims", response_model=SupplierPage,
            summary="Очередь заявок на подтверждение")
def claims(user: User = Depends(security.admin_only),
           page: int = Query(1, ge=1),
           page_size: int = Query(200, ge=1, le=500)) -> SupplierPage:
    """Всё, что менеджеры заявили и что ждёт фиксации. Только администратору."""
    where, values = _filters("", "", 0, "draft")
    total = db.fetch_one(f"{_LIST_CTE} SELECT COUNT(*) AS n FROM j{where}", values)
    rows = db.fetch_all(
        f"{_LIST_CTE} SELECT * FROM j{where} ORDER BY j.recipient LIMIT %s OFFSET %s",
        [*values, page_size, (page - 1) * page_size])
    return SupplierPage(items=[_entry(row) for row in rows],
                        total=int(total["n"]) if total else 0,
                        page=page, page_size=page_size)


@router.get("/conflicts", response_model=list[dict],
            summary="Поставщики, которым я платил, не будучи закреплённым")
def conflicts(user: User = Depends(security.current_user)) -> list[dict]:
    """Оплаты в пользу чужих поставщиков.

    Не ошибка и ничего не блокирует: подменить коллегу в отпуске — обычное
    дело. Но чаще это признак незаявленного закрепления, и человеку полезно
    увидеть свой список раньше, чем расхождение заметит кто-то другой.

    Считается только по себе: подсматривать, кто из коллег платил мимо своих
    поставщиков, вкладка оплат не должна.
    """
    return [
        {"recipient_key": row["recipient_key"], "recipient": row["recipient"],
         "payments": int(row["n"]), "amount": float(row["amount"] or 0),
         "assigned": list(row["assigned"] or [])}
        for row in db.fetch_all(
            "SELECT p.recipient_key, MAX(p.recipient) AS recipient,"
            "       COUNT(*) AS n, SUM(p.amount) AS amount,"
            "       (SELECT array_agg(u.full_name ORDER BY u.full_name)"
            "          FROM supplier_assignment a JOIN app_user u ON u.id = a.user_id"
            "         WHERE a.recipient_key = p.recipient_key) AS assigned"
            " FROM payment p"
            " WHERE p.recipient <> '' AND p.responsible = ANY(%s)"
            "   AND NOT EXISTS (SELECT 1 FROM supplier_assignment a"
            "                   WHERE a.recipient_key = p.recipient_key"
            "                     AND a.user_id = %s)"
            " GROUP BY p.recipient_key ORDER BY SUM(p.amount) DESC",
            (list(user.responsible), user.id))
    ]


@router.post("/claims", response_model=AssignmentResult,
             summary="Заявить поставщиков своими")
def claim(form: ClaimIn,
          user: User = Depends(security.current_user)) -> AssignmentResult:
    """Менеджер отмечает своих поставщиков. Пачкой — иначе разметка нереальна.

    Заявка ничего не отнимает у коллег: закрепление — связь, а не владение, и
    одного поставщика ведут несколько человек по разным брендам.
    """
    keys, skipped = _known(form.keys)
    if not keys:
        return AssignmentResult(changed=0, skipped=skipped)

    with db.cursor() as handle:
        handle.execute(
            "INSERT INTO supplier_assignment (recipient_key, user_id, claimed_by)"
            " SELECT k, %s::bigint, %s::bigint FROM unnest(%s::text[]) AS k"
            " ON CONFLICT (recipient_key, user_id) DO NOTHING"
            " RETURNING recipient_key",
            (user.id, user.id, keys))
        added = [row["recipient_key"] for row in handle.fetchall()]
        _sync_directions(handle, added)
        _log(handle, added, user.id, "claim", user.id)

    # Уже заявленное молча пропускается: человек, повторно выделивший своих
    # поставщиков, не должен получать ошибку на ровном месте.
    skipped.extend(key for key in keys if key not in set(added))
    return AssignmentResult(changed=len(added), skipped=skipped)


@router.post("/claims/withdraw", response_model=AssignmentResult,
             summary="Отозвать свою заявку")
def withdraw(form: ClaimIn,
             user: User = Depends(security.current_user)) -> AssignmentResult:
    """Отозвать можно только своё и только до фиксации."""
    with db.cursor() as handle:
        handle.execute(
            "DELETE FROM supplier_assignment"
            " WHERE recipient_key = ANY(%s) AND user_id = %s AND state = 'draft'"
            " RETURNING recipient_key",
            (form.keys, user.id))
        removed = [row["recipient_key"] for row in handle.fetchall()]
        _sync_directions(handle, removed)
        _log(handle, removed, user.id, "withdraw", user.id)

    skipped = [key for key in form.keys if key not in set(removed)]
    return AssignmentResult(changed=len(removed), skipped=skipped)


@router.post("/assignments/fix", response_model=AssignmentResult,
             summary="Зафиксировать закрепление")
def fix(form: AssignIn,
        user: User = Depends(security.admin_only)) -> AssignmentResult:
    """Администратор подтверждает заявки. После этого правит только он."""
    where = "recipient_key = ANY(%s) AND state = 'draft'"
    values: list[Any] = [form.keys]
    if form.user_ids:
        where += " AND user_id = ANY(%s)"
        values.append(form.user_ids)

    with db.cursor() as handle:
        handle.execute(
            "UPDATE supplier_assignment"
            " SET state = 'fixed', fixed_at = now(), fixed_by = %s"
            f" WHERE {where} RETURNING recipient_key, user_id",
            (user.id, *values))
        fixed = handle.fetchall()
        keys = [row["recipient_key"] for row in fixed]
        _sync_directions(handle, keys)
        for row in fixed:
            _log(handle, [row["recipient_key"]], row["user_id"], "fix", user.id)

    skipped = [key for key in form.keys if key not in set(keys)]
    return AssignmentResult(changed=len(fixed), skipped=skipped)


@router.post("/assignments/unassign", response_model=AssignmentResult,
             summary="Снять закрепление")
def unassign(form: AssignIn,
             user: User = Depends(security.admin_only)) -> AssignmentResult:
    """Снять закрепление, в том числе зафиксированное. Только администратор."""
    where = "recipient_key = ANY(%s)"
    values: list[Any] = [form.keys]
    if form.user_ids:
        where += " AND user_id = ANY(%s)"
        values.append(form.user_ids)

    with db.cursor() as handle:
        handle.execute(
            f"DELETE FROM supplier_assignment WHERE {where}"
            " RETURNING recipient_key, user_id, state",
            values)
        removed = handle.fetchall()
        keys = [row["recipient_key"] for row in removed]
        _sync_directions(handle, keys)
        for row in removed:
            _log(handle, [row["recipient_key"]], row["user_id"], "unfix", user.id,
                 {"state": row["state"]})

    return AssignmentResult(changed=len(removed),
                            skipped=[k for k in form.keys if k not in set(keys)])


@router.patch("/{recipient_key}/directions", response_model=list[str],
              summary="Задать направления вручную")
def set_directions(recipient_key: str, form: DirectionsIn,
                   user: User = Depends(security.admin_only)) -> list[str]:
    """Ручная правка направлений.

    Заданное руками помечается `manual` и пересчётом больше не трогается: иначе
    поставщик, которого администратор сознательно оставил в одном направлении,
    возвращался бы в оба при следующем закреплении.

    Пустой список возвращает поставщика под автоматическое правило.
    """
    known = {row["code"]: row["id"] for row in db.fetch_all(
        "SELECT id, code FROM direction WHERE is_active")}
    unknown = [code for code in form.codes if code not in known]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Неизвестное направление: {', '.join(unknown)}")

    with db.cursor() as handle:
        handle.execute("DELETE FROM supplier_direction WHERE recipient_key = %s",
                       (recipient_key,))
        if form.codes:
            handle.execute(
                "INSERT INTO supplier_direction"
                " (recipient_key, direction_id, source, updated_by)"
                " SELECT %s::text, id, 'manual', %s::bigint"
                " FROM unnest(%s::smallint[]) AS id",
                (recipient_key, user.id, [known[code] for code in form.codes]))
        else:
            _sync_directions(handle, [recipient_key])
        _log(handle, [recipient_key], None, "directions", user.id,
             {"codes": form.codes})
    return form.codes


# --- вспомогательное -----------------------------------------------------------

def _entry(row: dict) -> SupplierEntry:
    return SupplierEntry(
        recipient_key=row["recipient_key"],
        recipient=row["recipient"],
        supplier_id=int(row["supplier_id"] or 0),
        payments=int(row["payments"] or 0),
        amount=float(row["amount"] or 0),
        last_pay=row["last_pay"],
        managers=list(row["managers"] or []),
        assigned=list(row["assigned"] or []),
        directions=list(row["directions"] or []),
        unassigned_payers=list(row["unassigned_payers"] or []),
    )


def _known(keys: Iterable[str]) -> tuple[list[str], list[str]]:
    """Делит ключи на существующих получателей и незнакомые.

    Закрепить можно только того, кто есть в оплатах или связан с карточкой:
    ключ, набранный с ошибкой, иначе завёл бы закрепление за поставщиком,
    которого не существует, и оно молча висело бы в базе.
    """
    wanted = [key for key in keys if key]
    if not wanted:
        return [], []
    rows = db.fetch_all(
        "SELECT k FROM unnest(%s::text[]) AS k"
        " WHERE EXISTS (SELECT 1 FROM payment p WHERE p.recipient_key = k)"
        "    OR EXISTS (SELECT 1 FROM recipient_link l WHERE l.recipient_key = k)",
        (wanted,))
    known = [row["k"] for row in rows]
    return known, [key for key in wanted if key not in set(known)]


def _sync_directions(handle: Any, keys: list[str]) -> None:
    """Пересчитывает направления поставщика по направлениям его менеджеров.

    Направление берётся и с черновиков тоже, а не только с зафиксированного.
    Иначе заявленный поставщик до подтверждения администратором оставался бы
    без направления и пропадал из отбора, который у менеджера стоит по
    умолчанию, — то есть исчезал бы ровно в тот момент, когда его заявили.

    Двумя запросами, а не одним: удаление и вставка в общем CTE видят одно и то
    же состояние таблицы, и строка, удалённая первым, не была бы вставлена
    вторым — она попала бы в конфликт с ещё видимой копией.
    """
    if not keys:
        return
    handle.execute(
        "DELETE FROM supplier_direction sd"
        " WHERE sd.recipient_key = ANY(%s) AND sd.source = 'auto'"
        "   AND NOT EXISTS (SELECT 1 FROM supplier_direction m"
        "                   WHERE m.recipient_key = sd.recipient_key"
        "                     AND m.source = 'manual')",
        (keys,))
    handle.execute(
        "INSERT INTO supplier_direction (recipient_key, direction_id, source)"
        " SELECT DISTINCT a.recipient_key, ud.direction_id, 'auto'"
        " FROM supplier_assignment a"
        " JOIN user_direction ud ON ud.user_id = a.user_id"
        " WHERE a.recipient_key = ANY(%s)"
        "   AND NOT EXISTS (SELECT 1 FROM supplier_direction m"
        "                   WHERE m.recipient_key = a.recipient_key"
        "                     AND m.source = 'manual')"
        " ON CONFLICT (recipient_key, direction_id) DO NOTHING",
        (keys,))


def _log(handle: Any, keys: list[str], user_id: int | None, action: str,
         actor_id: int, payload: dict | None = None) -> None:
    """Журнал закреплений.

    Пишется всегда: база общая, и разбирательство «кто снял с меня поставщика»
    без журнала упирается в слово против слова.
    """
    if not keys:
        return
    # Типы указаны явно: в INSERT ... SELECT нетипизированный параметр
    # становится text, а приведения text → bigint при вставке не существует.
    handle.execute(
        "INSERT INTO supplier_assignment_log"
        " (recipient_key, user_id, action, payload, actor_id)"
        " SELECT k, %s::bigint, %s::text, %s::jsonb, %s::bigint"
        " FROM unnest(%s::text[]) AS k",
        (user_id, action, json.dumps(payload or {}, ensure_ascii=False),
         actor_id, keys))
