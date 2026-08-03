"""Сценарии отчётности: собрать исходники, построить сводную, выгрузить файл.

Интерфейс вызывает только эти функции — они рассчитаны на работу из фоновой
задачи и сообщают о ходе через `progress`. Разбор, правила магазинов, сводная и
оформление живут в своих модулях; здесь они связаны в тот порядок, в котором
раньше выполнялись руками.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Sequence

from . import data, export, pivot, source as source_module
from .models import ReportProfile, ReportTable, SaleRow, StoreRule
from .source import ColumnMap, SourceFile
from .stores import StoreMap, unknown_sources

Progress = Callable[[int, int], None]

# Расширения, которые вкладка предлагает как исходники.
SOURCE_EXTENSIONS = (".xlsx", ".xlsm", ".xls")

# Папка по умолчанию рядом с рабочими файлами приложения.
DEFAULT_FOLDER = "Для отчётности"


@dataclass(slots=True)
class BuildResult:
    """Всё, что нужно показать после сборки: таблица, переносы и предупреждения."""

    table: ReportTable
    mapping: StoreMap = field(default_factory=StoreMap)
    sources: list[SourceFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.table.empty

    @property
    def failed_sources(self) -> list[SourceFile]:
        return [item for item in self.sources if item.skipped < 0]


# --- настройки ------------------------------------------------------------------

def profiles() -> list[ReportProfile]:
    return data.ensure_default()


def save_profile(profile: ReportProfile) -> ReportProfile:
    return data.save_profile(profile)


def delete_profile(profile_id: int) -> None:
    data.delete_profile(profile_id)


def rules() -> list[StoreRule]:
    return data.list_rules()


def save_rule(rule: StoreRule) -> StoreRule:
    return data.save_rule(rule)


def delete_rule(rule_id: int) -> None:
    data.delete_rule(rule_id)


def online() -> bool:
    return data.online()


# --- разбор исходников ------------------------------------------------------------

def scan_folder(folder: str) -> list[str]:
    """Файлы-исходники в папке.

    Временные файлы Excel («~$отчёт.xlsx») отсеиваются: они появляются рядом с
    открытой книгой, читаются как повреждённые и каждый раз давали бы ошибку
    ровно там, где пользователь ничего не делал.
    """
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [os.path.join(folder, name) for name in names
            if name.lower().endswith(SOURCE_EXTENSIONS) and not name.startswith("~$")]


def read_sources(
    paths: Sequence[str],
    *,
    store_hints: dict[str, str] | None = None,
    mappings: dict[str, ColumnMap] | None = None,
    progress: Progress | None = None,
) -> list[SourceFile]:
    """Читает исходники по одному, сообщая о ходе: файлов бывает полтора десятка."""
    result: list[SourceFile] = []
    total = len(paths)
    hints = store_hints or {}
    known = mappings or {}
    for index, path in enumerate(paths):
        result.extend(source_module.collect(
            [path], store_hints=hints, mappings=known))
        if progress:
            progress(index + 1, total)
    return result


def inspect(path: str, sheet: str | None = None) -> SourceFile:
    """Разбирает один файл — для предпросмотра разметки колонок."""
    return source_module.read_source(path, sheet=sheet)


# --- сборка отчёта ----------------------------------------------------------------

def build(
    sources: Sequence[SourceFile],
    profile: ReportProfile,
    store_rules: Iterable[StoreRule] | None = None,
) -> BuildResult:
    """Строит отчёт из уже прочитанных исходников.

    Правила читаются здесь, а не передаются интерфейсом, если их не задали явно:
    так отчёт нельзя случайно собрать по устаревшему набору, оставшемуся в
    памяти окна с прошлого открытия.
    """
    known = list(store_rules) if store_rules is not None else rules()
    rows: list[SaleRow] = []
    warnings: list[str] = []
    for item in sources:
        if item.skipped < 0:
            warnings.append(f"{item.name}: не удалось прочитать — {item.sheet}")
            continue
        if not item.mapping.usable:
            missing = ", ".join(role.title for role in item.mapping.missing)
            warnings.append(
                f"{item.name}: не найдены обязательные колонки ({missing or 'шапка'})"
                " — файл пропущен")
            continue
        rows.extend(item.rows)

    # Названия до переноса запоминаются здесь: после сборки в таблице остаются
    # только приёмники, и «правило не на что применить» стало бы неотличимо от
    # «правило сработало».
    original = {row.source_store or row.store for row in rows if row.store}
    table, mapping = pivot.build(
        rows, profile, known,
        source_files=[item.name for item in sources if item.skipped >= 0])
    warnings.extend(_rule_warnings(mapping, known, original))
    if not rows:
        warnings.append("В исходных файлах не нашлось ни одной строки продаж")
    elif table.empty:
        warnings.append(
            "После фильтров не осталось ни одной строки. "
            "Проверьте отбор в профиле — например, «только акционные продажи»")
    return BuildResult(table=table, mapping=mapping, sources=list(sources),
                       warnings=warnings)


def _rule_warnings(mapping: StoreMap, known: Sequence[StoreRule],
                   original_stores: set[str]) -> list[str]:
    warnings: list[str] = []
    for loop in mapping.cycles:
        warnings.append(
            "Правила магазинов зациклены: " + " → ".join(loop) +
            ". Пока цикл не разорван, переносы не применяются")
    if mapping.valid and original_stores:
        if missing := unknown_sources(known, original_stores):
            warnings.append(
                "Правила есть, а магазинов в данных нет: " + ", ".join(missing))
    return warnings


def build_from_paths(
    paths: Sequence[str],
    profile: ReportProfile,
    *,
    store_hints: dict[str, str] | None = None,
    progress: Progress | None = None,
) -> BuildResult:
    """Полный путь «файлы → готовая сводная» одной операцией."""
    sources = read_sources(paths, store_hints=store_hints, progress=progress)
    return build(sources, profile)


# --- выгрузка -----------------------------------------------------------------------

def save(table: ReportTable, destination: str, *, author: str = "",
         today: date | None = None) -> str:
    return export.save(table, destination, author=author, today=today)


def suggested_path(table: ReportTable, folder: str) -> str:
    return export.default_name(table, folder)
