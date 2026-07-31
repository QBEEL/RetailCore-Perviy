"""Быстрая смена цен: подготовка файла загрузки цен в 1С по прайсу поставщика.

Слои независимы и не знают ни об интерфейсе, ни о конкретном поставщике:

* `onec`      — чтение шаблона выгрузки 1С и его видов цен;
* `supplier`  — чтение прайса поставщика;
* `mapping`   — соответствие колонок и сохраняемые профили поставщиков;
* `matching`  — подбор товара общим движком сопоставления приложения;
* `compare`   — сравнение старых цен с новыми;
* `export`    — запись новых цен в копию шаблона;
* `service`   — сценарий целиком, с журналом.
"""
from __future__ import annotations

from .compare import compare, describe, mapped_types
from .export import ExportReport, export
from .mapping import (
    SupplierProfile,
    SupplierProfiles,
    suggest_price_map,
    suggest_profile_name,
)
from .matching import MatchOptions, choices, match_lines
from .models import (
    PRICE_STATUS_TONES,
    PriceCell,
    PriceLine,
    PriceStats,
    PriceStatus,
    PriceType,
)
from .onec import OneCTemplate, load_template
from .service import (
    ComparisonResult,
    default_export_path,
    prepare_profile,
    read_supplier,
    read_template,
    recompare,
    run_comparison,
    save_result,
)
from .supplier import SupplierColumn, SupplierPrice, as_price, load_supplier

__all__ = [
    "PRICE_STATUS_TONES",
    "ComparisonResult",
    "ExportReport",
    "MatchOptions",
    "OneCTemplate",
    "PriceCell",
    "PriceLine",
    "PriceStats",
    "PriceStatus",
    "PriceType",
    "SupplierColumn",
    "SupplierPrice",
    "SupplierProfile",
    "SupplierProfiles",
    "as_price",
    "choices",
    "compare",
    "default_export_path",
    "describe",
    "export",
    "load_supplier",
    "load_template",
    "mapped_types",
    "match_lines",
    "prepare_profile",
    "read_supplier",
    "read_template",
    "recompare",
    "run_comparison",
    "save_result",
    "suggest_price_map",
    "suggest_profile_name",
]
