"""Сценарий переоценки целиком: чтение файлов, подбор, сравнение, выгрузка.

Здесь собраны длительные операции, которые страница запускает в фоновом потоке.
Каждая пишет строку в журнал `price.log` рядом с настройками: когда через месяц
выяснится, что цена ушла не туда, по журналу видно, какие файлы сравнивались,
какое соответствие колонок применялось и сколько строк не нашлось.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..appdata import log_event
from .compare import compare, describe
from .export import ExportReport, export
from .mapping import SupplierProfile, SupplierProfiles, suggest_price_map, suggest_profile_name
from .matching import MatchOptions, match_lines
from .models import PriceLine, PriceStats, PriceType
from .onec import OneCTemplate, load_template
from .supplier import SupplierPrice, load_supplier

LOG_FILE = "price.log"
ProgressCallback = Callable[[int, int], None]


@dataclass(slots=True)
class ComparisonResult:
    """Результат сравнения — всё, что нужно показать на странице."""

    lines: list[PriceLine] = field(default_factory=list)
    stats: PriceStats = field(default_factory=PriceStats)
    types: list[PriceType] = field(default_factory=list)
    profile: SupplierProfile = field(default_factory=SupplierProfile)


def read_template(path: str, sheet: str | None = None, progress: ProgressCallback | None = None) -> OneCTemplate:
    template = load_template(path, sheet, progress)
    log_event(LOG_FILE, (
        f"Шаблон 1С: {os.path.basename(path)} · лист «{template.sheet_name}» · "
        f"{len(template.records)} строк · виды цен: "
        f"{', '.join(t.name for t in template.valid_types) or 'не найдены'}"))
    return template


def read_supplier(
    path: str,
    sheet: str | None = None,
    profile: SupplierProfile | None = None,
    progress: ProgressCallback | None = None,
) -> SupplierPrice:
    supplier = load_supplier(path, sheet, None, progress)
    if profile is not None and (overrides := profile.overrides(supplier)):
        # Ручные роли применяются повторным разбором: роль колонки влияет на
        # нормализованные представления, которые считаются при чтении.
        supplier = load_supplier(path, sheet, overrides, progress)
    log_event(LOG_FILE, (
        f"Прайс поставщика: {os.path.basename(path)} · лист «{supplier.sheet_name}» · "
        f"{len(supplier.records)} строк · ценовые колонки: "
        f"{', '.join(c.title for c in supplier.price_columns) or 'не найдены'}"))
    return supplier


def prepare_profile(
    template: OneCTemplate,
    supplier: SupplierPrice,
    profiles: SupplierProfiles,
) -> SupplierProfile:
    """Профиль поставщика: сохранённый или подобранный автоматически."""
    saved = profiles.for_file(supplier.path)
    if saved is not None:
        profile = SupplierProfile(
            name=saved.name,
            sheet=supplier.sheet_name,
            price_map=dict(saved.price_map),
            role_map=dict(saved.role_map),
            separators=saved.separators,
            modifier_separators=saved.modifier_separators,
        )
        # Вид цены мог появиться в шаблоне после сохранения профиля.
        for name, column in suggest_price_map(template.valid_types, supplier.price_columns).items():
            profile.price_map.setdefault(name, column)
        return profile
    return SupplierProfile(
        name=suggest_profile_name(supplier.path),
        sheet=supplier.sheet_name,
        price_map=suggest_price_map(template.valid_types, supplier.price_columns),
    )


def run_comparison(
    template: OneCTemplate,
    supplier: SupplierPrice,
    profile: SupplierProfile,
    options: MatchOptions | None = None,
    progress: ProgressCallback | None = None,
) -> ComparisonResult:
    """Сопоставляет товары и сравнивает цены. Выполняется в фоновом потоке."""
    options = options or MatchOptions()
    lines = match_lines(template, supplier, options, progress)
    stats = compare(lines, template, supplier, profile)
    types = template.valid_types
    log_event(LOG_FILE, (
        f"Сравнение: {stats.total} строк · найдено {stats.found} ({stats.rate:.1f} %) · "
        f"изменено {stats.changed} · без изменений {stats.unchanged} · "
        f"требует сопоставления {stats.review} · не найдено {stats.not_found}\n"
        + "\n".join(f"  {describe(t, profile)}" for t in types)))
    return ComparisonResult(lines=lines, stats=stats, types=types, profile=profile)


def recompare(
    lines: Sequence[PriceLine],
    template: OneCTemplate,
    supplier: SupplierPrice,
    profile: SupplierProfile,
) -> PriceStats:
    """Пересчёт цен без повторного подбора: после ручной привязки или смены колонок."""
    return compare(lines, template, supplier, profile)


def save_result(
    template: OneCTemplate,
    lines: Sequence[PriceLine],
    destination: str,
    *,
    skip_unchanged: bool = False,
    progress: ProgressCallback | None = None,
) -> ExportReport:
    report = export(
        template, lines, destination,
        skip_unchanged=skip_unchanged, progress=progress)
    log_event(LOG_FILE, (
        f"Выгрузка: {report.file_name} · строк с новой ценой {report.rows} · "
        f"ячеек {report.cells} · удалено строк {report.removed}"))
    return report


def default_export_path(template: OneCTemplate) -> str:
    """Имя файла по умолчанию — рядом с шаблоном, с пометкой о переоценке."""
    directory = os.path.dirname(template.path)
    stem = os.path.splitext(os.path.basename(template.path))[0]
    return os.path.join(directory, f"{stem}_новые цены.xlsx")
