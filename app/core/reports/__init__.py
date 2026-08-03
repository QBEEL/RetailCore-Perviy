"""Отчётность для поставщиков: сводная по акциям и её оформление.

Заменяет ежемесячную ручную цепочку «выгрузка 1С → сводная таблица → удалить
лишнее → сохранить под именем месяца». Формат описывается профилем отчёта, а
правила «магазин-источник → магазин-приёмник» общие для всех менеджеров — и то,
и другое живёт в общей базе, локальная используется без подключения к серверу.

Интерфейс обращается к `service`; разбор исходников, сводная и оформление
разделены и проверяются по отдельности.
"""
from __future__ import annotations

from .models import (
    Cell,
    ColumnGroup,
    Field,
    GROUP_FIELDS,
    MONTHS,
    Metric,
    Period,
    ReportFilter,
    ReportProfile,
    ReportRow,
    ReportTable,
    SaleRow,
    StoreRule,
    as_fields,
    as_metrics,
    default_profile,
    parse_field,
    parse_metric,
    period_of,
)
from .source import ColumnMap, SourceFile, detect_mapping, read_source, store_from_name
from .stores import StoreMap, apply_rules, build_map, describe, normalize
from .service import BuildResult, DEFAULT_FOLDER, SOURCE_EXTENSIONS

__all__ = [
    "BuildResult",
    "Cell",
    "ColumnGroup",
    "ColumnMap",
    "DEFAULT_FOLDER",
    "Field",
    "GROUP_FIELDS",
    "MONTHS",
    "Metric",
    "Period",
    "ReportFilter",
    "ReportProfile",
    "ReportRow",
    "ReportTable",
    "SOURCE_EXTENSIONS",
    "SaleRow",
    "SourceFile",
    "StoreMap",
    "StoreRule",
    "apply_rules",
    "as_fields",
    "as_metrics",
    "build_map",
    "default_profile",
    "describe",
    "detect_mapping",
    "normalize",
    "parse_field",
    "parse_metric",
    "period_of",
    "read_source",
    "store_from_name",
]
