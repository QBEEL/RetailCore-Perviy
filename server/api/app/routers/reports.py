"""Отчётность для поставщиков: профили отчётов и правила объединения магазинов.

Настройки общие: профиль поставщика и правило «магазин-источник → магазин-
приёмник» видит и правит любой пользователь, а не только автор. В этом весь
смысл общей базы — иначе у каждого менеджера накопился бы свой набор поправок,
и сводная по сети перестала бы сходиться.

Удаление ограничено администратором. Правка — нет: поправить опечатку в правиле
должен уметь тот, кто её заметил, а вот убрать правило, на которое опираются
чужие отчёты, — это решение уровня отдела.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from .. import db, security
from ..schemas import ReportProfileIn, ReportProfileOut, StoreRuleIn, StoreRuleOut
from ..security import User

router = APIRouter(prefix="/api/reports", tags=["Отчётность"])

_PROFILE_FIELDS = ("SELECT p.id, p.name, p.supplier, p.supplier_id, p.payload,"
                   "       p.updated_at, COALESCE(u.full_name, '') AS updated_by"
                   " FROM report_profile p"
                   " LEFT JOIN app_user u ON u.id = p.updated_by")

_RULE_FIELDS = ("SELECT r.id, r.source, r.target, r.enabled, r.comment,"
                "       r.updated_at, COALESCE(u.full_name, '') AS updated_by"
                " FROM store_rule r"
                " LEFT JOIN app_user u ON u.id = r.updated_by")


# --- профили отчётов --------------------------------------------------------------

@router.get("/profiles", response_model=list[ReportProfileOut],
            summary="Все профили отчётов")
def list_profiles(user: User = Depends(security.current_user)
                  ) -> list[ReportProfileOut]:
    return [ReportProfileOut(**row) for row in db.fetch_all(
        f"{_PROFILE_FIELDS} ORDER BY p.name")]


@router.post("/profiles", response_model=ReportProfileOut,
             status_code=status.HTTP_201_CREATED, summary="Создать профиль")
def create_profile(form: ReportProfileIn,
                   user: User = Depends(security.current_user)) -> ReportProfileOut:
    _check_name(form.name)
    if _by_name(form.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Профиль «{form.name}» уже заведён")
    row = db.fetch_one(
        "WITH saved AS ("
        "  INSERT INTO report_profile (name, supplier, supplier_id, payload, updated_by)"
        "  VALUES (btrim(%s), %s, %s, %s, %s) RETURNING *)"
        " SELECT s.id, s.name, s.supplier, s.supplier_id, s.payload, s.updated_at,"
        "        COALESCE(u.full_name, '') AS updated_by"
        " FROM saved s LEFT JOIN app_user u ON u.id = s.updated_by",
        (form.name, form.supplier, form.supplier_id, _json(form.payload), user.id))
    _log(user.id, "report_profile", row["id"], "create", {"name": form.name})
    return ReportProfileOut(**row)


@router.patch("/profiles/{profile_id}", response_model=ReportProfileOut,
              summary="Изменить профиль")
def update_profile(profile_id: int, form: ReportProfileIn,
                   user: User = Depends(security.current_user)) -> ReportProfileOut:
    _check_name(form.name)
    existing = _by_name(form.name)
    if existing and existing["id"] != profile_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Профиль «{form.name}» уже заведён")
    row = db.fetch_one(
        "WITH saved AS ("
        "  UPDATE report_profile SET name = btrim(%s), supplier = %s,"
        "         supplier_id = %s, payload = %s, updated_at = now(), updated_by = %s"
        "  WHERE id = %s RETURNING *)"
        " SELECT s.id, s.name, s.supplier, s.supplier_id, s.payload, s.updated_at,"
        "        COALESCE(u.full_name, '') AS updated_by"
        " FROM saved s LEFT JOIN app_user u ON u.id = s.updated_by",
        (form.name, form.supplier, form.supplier_id, _json(form.payload),
         user.id, profile_id))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Профиль не найден")
    _log(user.id, "report_profile", profile_id, "update", {"name": form.name})
    return ReportProfileOut(**row)


@router.delete("/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Удалить профиль")
def delete_profile(profile_id: int,
                   user: User = Depends(security.admin_only)) -> None:
    if not db.execute("DELETE FROM report_profile WHERE id = %s", (profile_id,)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Профиль не найден")
    _log(user.id, "report_profile", profile_id, "delete", {})


# --- правила объединения магазинов --------------------------------------------------

@router.get("/store-rules", response_model=list[StoreRuleOut],
            summary="Все правила «магазин → магазин»")
def list_rules(user: User = Depends(security.current_user)) -> list[StoreRuleOut]:
    return [StoreRuleOut(**row) for row in db.fetch_all(
        f"{_RULE_FIELDS} ORDER BY r.source")]


@router.put("/store-rules", response_model=StoreRuleOut, summary="Сохранить правило")
def save_rule(form: StoreRuleIn,
              user: User = Depends(security.current_user)) -> StoreRuleOut:
    source, target = form.source.strip(), form.target.strip()
    if not source or not target:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="В правиле должны быть указаны и источник, и приёмник")
    if _key(source) == _key(target):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Магазин нельзя объединить сам с собой")
    if loop := _cycle_through(source, target):
        # Цикл ловится до записи: с ним переносы не применяются вовсе, и
        # менеджер получил бы отчёт по старым правилам без единого объяснения.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Правило замкнёт цепочку в кольцо: " + " → ".join(loop))

    row = db.fetch_one(
        "WITH saved AS ("
        "  INSERT INTO store_rule (source_key, source, target, enabled, comment,"
        "                          updated_by)"
        "  VALUES (%s, %s, %s, %s, %s, %s)"
        "  ON CONFLICT (source_key) DO UPDATE"
        "  SET source = EXCLUDED.source, target = EXCLUDED.target,"
        "      enabled = EXCLUDED.enabled, comment = EXCLUDED.comment,"
        "      updated_at = now(), updated_by = EXCLUDED.updated_by"
        "  RETURNING *)"
        " SELECT s.id, s.source, s.target, s.enabled, s.comment, s.updated_at,"
        "        COALESCE(u.full_name, '') AS updated_by"
        " FROM saved s LEFT JOIN app_user u ON u.id = s.updated_by",
        (_key(source), source, target, form.enabled, form.comment, user.id))
    _log(user.id, "store_rule", row["id"], "save",
         {"source": source, "target": target, "enabled": form.enabled})
    return StoreRuleOut(**row)


@router.delete("/store-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Удалить правило")
def delete_rule(rule_id: int, user: User = Depends(security.admin_only)) -> None:
    row = db.fetch_one("SELECT source, target FROM store_rule WHERE id = %s",
                       (rule_id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Правило не найдено")
    db.execute("DELETE FROM store_rule WHERE id = %s", (rule_id,))
    _log(user.id, "store_rule", rule_id, "delete",
         {"source": row["source"], "target": row["target"]})


# --- вспомогательное ----------------------------------------------------------------

def _key(name: str) -> str:
    """Ключ сравнения названий. Повторяет `normalize` из приложения —
    иначе одно и то же правило заводилось бы дважды в разном написании."""
    return " ".join(str(name or "").split()).lower().replace("ё", "е")


def _json(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


def _by_name(name: str) -> dict | None:
    return db.fetch_one(
        "SELECT id FROM report_profile WHERE lower(btrim(name)) = lower(btrim(%s))",
        (name,))


def _check_name(name: str) -> None:
    if not name.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="У профиля отчёта должно быть название")


def _cycle_through(source: str, target: str) -> list[str]:
    """Пройдёт ли цепочка от нового приёмника обратно к источнику.

    Существующие правила читаются целиком: их десятки, а не тысячи, и один
    запрос дешевле рекурсивного обхода в базе.
    """
    direct = {row["source_key"]: row["target"] for row in db.fetch_all(
        "SELECT source_key, target FROM store_rule WHERE enabled")}
    direct[_key(source)] = target

    chain = [source]
    seen = {_key(source)}
    current = _key(target)
    while True:
        chain.append(_name_of(direct, current) or current)
        if current in seen:
            return chain
        seen.add(current)
        following = direct.get(current)
        if following is None:
            return []
        current = _key(following)


def _name_of(direct: dict[str, str], key: str) -> str:
    """Написание названия, как его задал пользователь, — для текста ошибки."""
    for target in direct.values():
        if _key(target) == key:
            return target
    return ""


def _log(user_id: int, entity: str, entity_id: int, action: str,
         changes: dict) -> None:
    import json

    db.execute(
        "INSERT INTO audit_log (user_id, entity, entity_id, action, changes)"
        " VALUES (%s, %s, %s, %s, %s::jsonb)",
        (user_id, entity, entity_id, action, json.dumps(changes, ensure_ascii=False)))
