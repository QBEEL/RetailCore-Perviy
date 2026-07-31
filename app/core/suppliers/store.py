"""Хранилище базы поставщиков: карточки, структуры прайсов, ручные привязки.

Соединение SQLite нельзя делить между потоками, поэтому каждая функция
открывает своё и закрывает за собой — так же, как в базе снимков: чтение идёт
из потока интерфейса, а сохранение привязок может уйти в фоновую задачу.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable, Iterator, Sequence

from .. import appdata
from ..normalize import normalize_text
from ..pricing.mapping import SupplierProfile
from . import schema
from .models import LinkKey, Supplier, SupplierLayout, SupplierLink

DB_FILE = "suppliers.db"


def database_path() -> str:
    return appdata.path_to(DB_FILE)


@contextmanager
def connect(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Открывает базу, при необходимости создавая её и приводя схему к версии."""
    target = path or database_path()
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    connection = sqlite3.connect(target)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        schema.migrate(connection)
        yield connection
    finally:
        connection.close()


def signature_of(titles: Sequence[str]) -> str:
    """Отпечаток структуры файла — набор заголовков без учёта их порядка.

    Порядок колонок поставщик меняет между выгрузками чаще, чем сами названия,
    поэтому отпечаток строится по отсортированному набору.
    """
    parts = sorted({normalized for title in titles if (normalized := normalize_text(title))})
    if not parts:
        return ""
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


# --- поставщики ---------------------------------------------------------------

def list_suppliers(path: str | None = None, *, active_only: bool = False) -> list[Supplier]:
    query = """
        SELECT s.*,
               (SELECT COUNT(*) FROM supplier_layout l WHERE l.supplier_id = s.id) AS layouts,
               (SELECT COUNT(*) FROM supplier_link k WHERE k.supplier_id = s.id) AS links
        FROM supplier s
    """
    if active_only:
        query += " WHERE s.active = 1"
    query += " ORDER BY s.name COLLATE NOCASE"
    with connect(path) as connection:
        rows = connection.execute(query).fetchall()
    return [_supplier(row) for row in rows]


def get_supplier(supplier_id: int, path: str | None = None) -> Supplier | None:
    with connect(path) as connection:
        row = connection.execute("SELECT * FROM supplier WHERE id = ?", (supplier_id,)).fetchone()
    return _supplier(row) if row is not None else None


def find_supplier(name: str, path: str | None = None) -> Supplier | None:
    key = normalize_text(name)
    if not key:
        return None
    with connect(path) as connection:
        row = connection.execute("SELECT * FROM supplier WHERE key = ?", (key,)).fetchone()
    return _supplier(row) if row is not None else None


def save_supplier(supplier: Supplier, path: str | None = None) -> Supplier:
    """Создаёт или обновляет карточку. Имя приводится к ключу и должно быть занято один раз."""
    if not supplier.name.strip():
        raise ValueError("У поставщика должно быть имя")
    now = _now()
    with connect(path) as connection:
        if supplier.id:
            connection.execute(
                """UPDATE supplier SET name = ?, key = ?, active = ?, brands = ?,
                          categories = ?, contact = ?, note = ?,
                          payment_terms_days = ?, updated_at = ?
                   WHERE id = ?""",
                (supplier.name.strip(), supplier.key, int(supplier.active), supplier.brands,
                 supplier.categories, supplier.contact, supplier.note,
                 int(supplier.payment_terms_days), now, supplier.id),
            )
        else:
            existing = connection.execute(
                "SELECT id FROM supplier WHERE key = ?", (supplier.key,)).fetchone()
            if existing is not None:
                supplier.id = int(existing["id"])
            else:
                cursor = connection.execute(
                    """INSERT INTO supplier (name, key, active, brands, categories,
                                             contact, note, payment_terms_days,
                                             created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (supplier.name.strip(), supplier.key, int(supplier.active), supplier.brands,
                     supplier.categories, supplier.contact, supplier.note,
                     int(supplier.payment_terms_days), now, now),
                )
                supplier.id = int(cursor.lastrowid)
        connection.commit()
    return supplier


def delete_supplier(supplier_id: int, path: str | None = None) -> bool:
    """Удаляет карточку вместе со структурами и привязками."""
    with connect(path) as connection:
        cursor = connection.execute("DELETE FROM supplier WHERE id = ?", (supplier_id,))
        connection.commit()
        return cursor.rowcount > 0


# --- имена для узнавания ------------------------------------------------------

def aliases(supplier_id: int, path: str | None = None) -> list[str]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT pattern FROM supplier_alias WHERE supplier_id = ? ORDER BY pattern",
            (supplier_id,)).fetchall()
    return [row["pattern"] for row in rows]


def set_aliases(supplier_id: int, patterns: Iterable[str], path: str | None = None) -> None:
    cleaned = {normalize_text(p): p.strip() for p in patterns if normalize_text(p)}
    with connect(path) as connection:
        connection.execute("DELETE FROM supplier_alias WHERE supplier_id = ?", (supplier_id,))
        connection.executemany(
            "INSERT INTO supplier_alias (supplier_id, pattern, key) VALUES (?, ?, ?)",
            [(supplier_id, pattern, key) for key, pattern in cleaned.items()])
        connection.commit()


def all_aliases(path: str | None = None) -> dict[int, list[str]]:
    """Ключи узнавания всех поставщиков разом — для подбора по имени файла."""
    with connect(path) as connection:
        rows = connection.execute("SELECT supplier_id, key FROM supplier_alias").fetchall()
    found: dict[int, list[str]] = {}
    for row in rows:
        found.setdefault(int(row["supplier_id"]), []).append(row["key"])
    return found


# --- структуры прайсов --------------------------------------------------------

def layouts(supplier_id: int, path: str | None = None) -> list[SupplierLayout]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM supplier_layout WHERE supplier_id = ?"
            " ORDER BY uses DESC, last_used_at DESC, id",
            (supplier_id,)).fetchall()
    return [_layout(row) for row in rows]


def all_layouts(path: str | None = None) -> list[SupplierLayout]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM supplier_layout ORDER BY uses DESC, id").fetchall()
    return [_layout(row) for row in rows]


def save_layout(layout: SupplierLayout, path: str | None = None) -> SupplierLayout:
    """Сохраняет структуру. Та же сигнатура и лист обновляют существующую запись."""
    if not layout.supplier_id:
        raise ValueError("Структура прайса не привязана к поставщику")
    now = _now()
    profile = layout.profile
    values = (
        json.dumps(layout.titles, ensure_ascii=False),
        json.dumps(profile.price_map, ensure_ascii=False),
        json.dumps(profile.role_map, ensure_ascii=False),
        profile.separators,
        profile.modifier_separators,
    )
    with connect(path) as connection:
        row = connection.execute(
            "SELECT id, uses FROM supplier_layout"
            " WHERE supplier_id = ? AND signature = ? AND sheet_name = ?",
            (layout.supplier_id, layout.signature, profile.sheet)).fetchone()
        if row is not None:
            layout.id = int(row["id"])
            layout.uses = int(row["uses"]) + 1
            connection.execute(
                """UPDATE supplier_layout SET titles = ?, price_map = ?, role_map = ?,
                          separators = ?, modifier_separators = ?, last_used_at = ?, uses = ?
                   WHERE id = ?""",
                (*values, now, layout.uses, layout.id))
        else:
            layout.uses = 1
            cursor = connection.execute(
                """INSERT INTO supplier_layout (supplier_id, sheet_name, signature, titles,
                                                price_map, role_map, separators,
                                                modifier_separators, created_at,
                                                last_used_at, uses)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (layout.supplier_id, profile.sheet, layout.signature, *values, now, now))
            layout.id = int(cursor.lastrowid)
        connection.commit()
    return layout


def delete_layout(layout_id: int, path: str | None = None) -> bool:
    with connect(path) as connection:
        cursor = connection.execute("DELETE FROM supplier_layout WHERE id = ?", (layout_id,))
        connection.commit()
        return cursor.rowcount > 0


def known_price_types(path: str | None = None) -> list[str]:
    """Виды цен, встречавшиеся в разобранных шаблонах, — от частых к редким.

    Виды цен принадлежат базе 1С, а не поставщику, поэтому список, собранный по
    всем структурам, годится для любого. Он позволяет завести прайс нового
    поставщика вручную, не открывая шаблон выгрузки заново.
    """
    counter: Counter[str] = Counter()
    with connect(path) as connection:
        rows = connection.execute("SELECT price_map FROM supplier_layout").fetchall()
    for row in rows:
        counter.update(_json_map(row["price_map"]).keys())
    return [name for name, _ in counter.most_common()]


# --- ручные привязки ----------------------------------------------------------

def links(supplier_id: int, path: str | None = None) -> list[SupplierLink]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM supplier_link WHERE supplier_id = ? ORDER BY created_at DESC, id DESC",
            (supplier_id,)).fetchall()
    return [_link(row) for row in rows]


def save_link(link: SupplierLink, path: str | None = None) -> SupplierLink:
    """Запоминает привязку. Повторная привязка той же строки заменяет прежнюю."""
    identity = link.key.identity
    if not identity:
        raise ValueError("Строку 1С нечем опознать — привязка не сохранена")
    if not link.supplier_id:
        raise ValueError("Привязка не отнесена к поставщику")
    with connect(path) as connection:
        connection.execute(
            """INSERT INTO supplier_link (supplier_id, identity, nomenclature, characteristic,
                                          article_key, volume, name_key, onec_article, onec_name,
                                          supplier_article, supplier_sku, supplier_ean,
                                          supplier_name, created_at, author, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(supplier_id, identity) DO UPDATE SET
                   nomenclature = excluded.nomenclature,
                   characteristic = excluded.characteristic,
                   article_key = excluded.article_key,
                   volume = excluded.volume,
                   name_key = excluded.name_key,
                   onec_article = excluded.onec_article,
                   onec_name = excluded.onec_name,
                   supplier_article = excluded.supplier_article,
                   supplier_sku = excluded.supplier_sku,
                   supplier_ean = excluded.supplier_ean,
                   supplier_name = excluded.supplier_name,
                   created_at = excluded.created_at,
                   author = excluded.author""",
            (link.supplier_id, identity, link.key.nomenclature, link.key.characteristic,
             link.key.article, link.key.volume, link.key.name, link.onec_article, link.onec_name,
             link.supplier_article, link.supplier_sku, link.supplier_ean, link.supplier_name,
             _now(), link.author or _user(), link.note))
        connection.commit()
        row = connection.execute(
            "SELECT * FROM supplier_link WHERE supplier_id = ? AND identity = ?",
            (link.supplier_id, identity)).fetchone()
    return _link(row)


def delete_link(link_id: int, path: str | None = None) -> bool:
    with connect(path) as connection:
        cursor = connection.execute("DELETE FROM supplier_link WHERE id = ?", (link_id,))
        connection.commit()
        return cursor.rowcount > 0


def clear_links(supplier_id: int, path: str | None = None) -> int:
    with connect(path) as connection:
        cursor = connection.execute(
            "DELETE FROM supplier_link WHERE supplier_id = ?", (supplier_id,))
        connection.commit()
        return cursor.rowcount


# --- перенос профилей из настроек ---------------------------------------------

def adopt_profiles(profiles: Iterable[SupplierProfile], path: str | None = None) -> int:
    """Переносит профили, сохранённые в settings.json до появления базы.

    Делается один раз, на пустой базе: иначе повторный запуск воскрешал бы
    поставщиков, которых пользователь успел удалить.
    """
    with connect(path) as connection:
        if connection.execute("SELECT 1 FROM supplier LIMIT 1").fetchone() is not None:
            return 0

    adopted = 0
    for profile in profiles:
        if not profile.name.strip():
            continue
        supplier = save_supplier(Supplier(name=profile.name), path)
        save_layout(
            SupplierLayout(supplier_id=supplier.id, profile=profile, signature="", titles=[]),
            path)
        adopted += 1
    return adopted


def database_size(path: str | None = None) -> int:
    target = path or database_path()
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(target + suffix)
        except OSError:
            pass
    return total


# --- преобразования -----------------------------------------------------------

def _supplier(row: sqlite3.Row) -> Supplier:
    keys = row.keys()
    return Supplier(
        id=int(row["id"]),
        name=row["name"],
        active=bool(row["active"]),
        brands=row["brands"],
        categories=row["categories"],
        contact=row["contact"],
        note=row["note"],
        payment_terms_days=int(row["payment_terms_days"]) if "payment_terms_days" in keys else 0,
        created_at=_moment(row["created_at"]),
        updated_at=_moment(row["updated_at"]),
        layouts=int(row["layouts"]) if "layouts" in keys else 0,
        links=int(row["links"]) if "links" in keys else 0,
    )


def _layout(row: sqlite3.Row) -> SupplierLayout:
    return SupplierLayout(
        id=int(row["id"]),
        supplier_id=int(row["supplier_id"]),
        signature=row["signature"],
        titles=_json_list(row["titles"]),
        uses=int(row["uses"]),
        created_at=_moment(row["created_at"]),
        last_used_at=_moment(row["last_used_at"]),
        profile=SupplierProfile(
            name="",
            sheet=row["sheet_name"],
            price_map=_json_map(row["price_map"]),
            role_map=_json_map(row["role_map"]),
            separators=row["separators"],
            modifier_separators=row["modifier_separators"],
        ),
    )


def _link(row: sqlite3.Row) -> SupplierLink:
    return SupplierLink(
        id=int(row["id"]),
        supplier_id=int(row["supplier_id"]),
        key=LinkKey(
            nomenclature=row["nomenclature"],
            characteristic=row["characteristic"],
            article=row["article_key"],
            volume=row["volume"],
            name=row["name_key"],
        ),
        onec_article=row["onec_article"],
        onec_name=row["onec_name"],
        supplier_article=row["supplier_article"],
        supplier_sku=row["supplier_sku"],
        supplier_ean=row["supplier_ean"],
        supplier_name=row["supplier_name"],
        created_at=_moment(row["created_at"]),
        author=row["author"],
        note=row["note"],
    )


def _json_map(text: Any) -> dict[str, str]:
    try:
        data = json.loads(text or "{}")
    except (ValueError, TypeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _json_list(text: Any) -> list[str]:
    try:
        data = json.loads(text or "[]")
    except (ValueError, TypeError):
        return []
    return [str(v) for v in data] if isinstance(data, list) else []


def _moment(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text))
    except (ValueError, TypeError):
        return None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — имя пользователя не критично для привязки
        return ""
