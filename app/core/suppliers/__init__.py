"""База поставщиков: карточки, структуры прайсов и сохранённые привязки.

У каждого поставщика свой файл переоценки и своя структура данных, поэтому
приложение не пытается свести всех к одному формату, а запоминает разбор
каждого: как читать его прайс и какие позиции пришлось свести вручную.

* `models`   — карточка поставщика, структура прайса, привязка и её ключ;
* `schema`   — таблицы `suppliers.db` и миграции;
* `store`    — чтение и запись;
* `identify` — чей это файл: имя, дополнительные имена, структура заголовков;
* `links`    — применение сохранённых привязок к строкам переоценки.

Слой стоит над `pricing` и зависит от него, но не наоборот: переоценка умеет
работать и с пустой базой.
"""
from __future__ import annotations

from .identify import identify, similarity, suggest_aliases
from .links import (
    NAME_DRIFT_THRESHOLD,
    STAGE_LINK,
    LinkBook,
    SupplierIndex,
    apply_links,
    keys_for,
    link_from,
)
from .models import Guess, LinkKey, Supplier, SupplierLayout, SupplierLink
from .service import (
    Session,
    adopt_settings_profiles,
    default_supplier_name,
    describe_database,
    forget_link,
    open_session,
    remember_link,
    remember_session,
    run_comparison,
)
from .store import (
    adopt_profiles,
    all_aliases,
    all_layouts,
    aliases,
    clear_links,
    database_path,
    database_size,
    delete_layout,
    delete_link,
    delete_supplier,
    find_supplier,
    get_supplier,
    known_price_types,
    layouts,
    links,
    list_suppliers,
    save_layout,
    save_link,
    save_supplier,
    set_aliases,
    signature_of,
)

__all__ = [
    "Guess",
    "LinkBook",
    "LinkKey",
    "NAME_DRIFT_THRESHOLD",
    "STAGE_LINK",
    "Session",
    "Supplier",
    "SupplierIndex",
    "SupplierLayout",
    "SupplierLink",
    "adopt_profiles",
    "adopt_settings_profiles",
    "aliases",
    "all_aliases",
    "all_layouts",
    "apply_links",
    "clear_links",
    "database_path",
    "database_size",
    "default_supplier_name",
    "delete_layout",
    "delete_link",
    "delete_supplier",
    "describe_database",
    "find_supplier",
    "forget_link",
    "get_supplier",
    "identify",
    "keys_for",
    "known_price_types",
    "layouts",
    "link_from",
    "links",
    "list_suppliers",
    "open_session",
    "remember_link",
    "remember_session",
    "run_comparison",
    "save_layout",
    "save_link",
    "save_supplier",
    "set_aliases",
    "signature_of",
    "similarity",
    "suggest_aliases",
]
