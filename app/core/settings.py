"""Хранение пользовательских настроек в JSON рядом с профилем пользователя."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .appdata import APP_NAME, path_to
from .matching import MatchConfig
from .models import DEFAULT_SEARCH_ROLES, DEFAULT_WEIGHTS, FieldRole
from .order import Alias, AliasBook
from .pricing import MatchOptions, SupplierProfile, SupplierProfiles
from .search import SearchConfig

MAX_RECENT = 10

# Поля, которые по умолчанию дозаполняются в целевом файле из каталога.
DEFAULT_FILL_ROLES: tuple[FieldRole, ...] = (FieldRole.EAN, FieldRole.ARTICLE, FieldRole.PRICE)


def settings_path() -> str:
    return path_to("settings.json")


@dataclass
class AppSettings:
    """Все сохраняемые предпочтения приложения."""

    search: SearchConfig = field(default_factory=SearchConfig)
    match: MatchConfig = field(default_factory=MatchConfig)
    fill_roles: list[FieldRole] = field(default_factory=lambda: list(DEFAULT_FILL_ROLES))
    overwrite_filled: bool = False
    recent_source: list[str] = field(default_factory=list)
    recent_target: list[str] = field(default_factory=list)
    extra_sources: list[str] = field(default_factory=list)
    recent_order_source: list[str] = field(default_factory=list)
    recent_order_target: list[str] = field(default_factory=list)
    order_sheets: dict[str, str] = field(default_factory=dict)
    order_aliases: AliasBook = field(default_factory=AliasBook)
    price_match: MatchOptions = field(default_factory=MatchOptions)
    price_skip_unchanged: bool = False
    recent_price_template: list[str] = field(default_factory=list)
    recent_price_supplier: list[str] = field(default_factory=list)
    price_sheets: dict[str, str] = field(default_factory=dict)
    supplier_profiles: SupplierProfiles = field(default_factory=SupplierProfiles)
    window_geometry: str = ""
    window_state: str = ""
    splitter_sizes: list[int] = field(default_factory=list)
    column_overrides: dict[str, dict[int, FieldRole]] = field(default_factory=dict)
    update_check_auto: bool = True
    update_download_auto: bool = True
    update_show_changelog: bool = True
    update_skip_version: str = ""
    update_remind_after: str = ""
    snapshots_enabled: bool = True
    recent_payment_import: list[str] = field(default_factory=list)
    # Пороги суммы за день для цветовой индикации календаря. По умолчанию
    # выбраны по истории оплат: шкала в сотнях тысяч красит красным семь дней
    # из десяти и перестаёт что-либо различать.
    payment_levels: list[float] = field(default_factory=lambda: [500_000.0, 1_500_000.0, 3_000_000.0])
    payment_budget_warn: float = 90.0
    payment_import_reminder: bool = True
    payment_import_seen: str = ""
    # Адрес общей базы оплат. Пустой означает работу со своей локальной базой —
    # так приложение вело себя до появления сервера, и так оно продолжает
    # работать у того, кому общий доступ не нужен.
    payment_server: str = "https://retail.qbeely.ru"
    payment_login: str = ""
    # Пароль здесь не хранится намеренно: он спрашивается при каждом запуске.
    _path: str = field(default_factory=settings_path, repr=False)

    @property
    def day_levels(self) -> tuple[float, float, float]:
        """Пороги в том виде, в каком их ждёт расчёт уровня дня."""
        values = sorted(float(v) for v in self.payment_levels[:3] if float(v) > 0)
        if len(values) != 3:
            return (500_000.0, 1_500_000.0, 3_000_000.0)
        return (values[0], values[1], values[2])

    @classmethod
    def load(cls, path: str | None = None) -> "AppSettings":
        target = path or settings_path()
        settings = cls(_path=target)
        try:
            with open(target, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return settings
        settings._apply(data)
        return settings

    def save(self) -> None:
        """Записывает настройки целиком и только после успешной сборки.

        Порядок здесь важнее краткости. Прежняя версия открывала файл на
        запись прямо в `with`, а данные собирала уже внутри: `open(..., "w")`
        обрезал файл сразу, и любая ошибка при сборке оставляла пользователя с
        пустым `settings.json` — без недавних файлов, весов, исключений и
        профилей. Сначала текст, потом временный файл, потом атомарная замена:
        оборвись запись хоть на середине, прежние настройки останутся целыми.
        """
        payload = json.dumps(self._as_dict(), ensure_ascii=False, indent=2)
        if directory := os.path.dirname(self._path):
            os.makedirs(directory, exist_ok=True)
        temporary = f"{self._path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary, self._path)

    def remember_file(self, path: str, *, source: bool) -> None:
        _remember(self.recent_source if source else self.recent_target, path)

    def remember_order_file(self, path: str, *, source: bool) -> None:
        _remember(self.recent_order_source if source else self.recent_order_target, path)

    def remember_price_file(self, path: str, *, template: bool) -> None:
        _remember(self.recent_price_template if template else self.recent_price_supplier, path)

    def price_sheet_for(self, path: str) -> str:
        return self.price_sheets.get(path, "")

    def remember_price_sheet(self, path: str, sheet: str) -> None:
        if sheet:
            self.price_sheets[path] = sheet

    def order_sheet_for(self, path: str) -> str:
        return self.order_sheets.get(path, "")

    def remember_order_sheet(self, path: str, sheet: str) -> None:
        """Выбранный лист запоминается на файл: в книге с двумя листами
        автоопределение не должно каждый раз переспорить пользователя."""
        if sheet:
            self.order_sheets[path] = sheet

    def overrides_for(self, path: str) -> dict[int, FieldRole]:
        return self.column_overrides.get(path, {})

    def set_overrides(self, path: str, overrides: dict[int, FieldRole]) -> None:
        """Запоминает ручную разметку колонок файла.

        Роли приводятся к перечислению на входе: из выпадающего списка Qt
        отдаёт обычную строку, а не FieldRole, и такое значение ломало бы уже
        сохранение — вместе со всеми остальными настройками.
        """
        cleaned = {
            int(index): role
            for index, value in overrides.items()
            if (role := _role(value)) is not None
        }
        if cleaned:
            self.column_overrides[path] = cleaned
        else:
            self.column_overrides.pop(path, None)

    def _apply(self, data: dict[str, Any]) -> None:
        search = data.get("search", {})
        if roles := search.get("roles"):
            self.search.roles = {r for name in roles if (r := _role(name))}
        for name, value in (search.get("weights") or {}).items():
            if role := _role(name):
                self.search.weights[role] = float(value)
        self.search.fuzzy_enabled = bool(search.get("fuzzy_enabled", self.search.fuzzy_enabled))
        self.search.fuzzy_threshold = float(search.get("fuzzy_threshold", self.search.fuzzy_threshold))
        self.search.min_score = float(search.get("min_score", self.search.min_score))

        match = data.get("match", {})
        self.match.enforce_volume = bool(match.get("enforce_volume", self.match.enforce_volume))
        self.match.volume_tolerance = float(match.get("volume_tolerance", self.match.volume_tolerance))
        self.match.fuzzy_threshold = float(match.get("fuzzy_threshold", self.match.fuzzy_threshold))
        self.match.auto_accept = float(match.get("auto_accept", self.match.auto_accept))

        if fill := data.get("fill_roles"):
            self.fill_roles = [r for name in fill if (r := _role(name))]
        self.overwrite_filled = bool(data.get("overwrite_filled", self.overwrite_filled))
        self.recent_source = [p for p in data.get("recent_source", []) if isinstance(p, str)]
        self.recent_target = [p for p in data.get("recent_target", []) if isinstance(p, str)]
        self.extra_sources = [p for p in data.get("extra_sources", []) if isinstance(p, str)]
        self.recent_order_source = [p for p in data.get("recent_order_source", []) if isinstance(p, str)]
        self.recent_order_target = [p for p in data.get("recent_order_target", []) if isinstance(p, str)]
        self.order_sheets = {
            path: sheet for path, sheet in (data.get("order_sheets") or {}).items()
            if isinstance(path, str) and isinstance(sheet, str)
        }
        self.order_aliases = AliasBook(
            Alias.from_dict(item) for item in (data.get("order_aliases") or [])
            if isinstance(item, dict)
        )
        price = data.get("price_match", {})
        for name in (
            "use_article", "use_ean", "use_sku", "use_name", "use_fuzzy",
            "ignore_case", "ignore_spaces", "ignore_symbols",
        ):
            if name in price:
                setattr(self.price_match, name, bool(price[name]))
        self.price_match.min_score = float(price.get("min_score", self.price_match.min_score))
        self.price_match.separators = str(price.get("separators", self.price_match.separators))
        self.price_match.modifier_separators = str(
            price.get("modifier_separators", self.price_match.modifier_separators))
        self.price_skip_unchanged = bool(data.get("price_skip_unchanged", self.price_skip_unchanged))
        self.recent_price_template = [p for p in data.get("recent_price_template", []) if isinstance(p, str)]
        self.recent_price_supplier = [p for p in data.get("recent_price_supplier", []) if isinstance(p, str)]
        self.price_sheets = {
            path: sheet for path, sheet in (data.get("price_sheets") or {}).items()
            if isinstance(path, str) and isinstance(sheet, str)
        }
        self.supplier_profiles = SupplierProfiles(
            SupplierProfile.from_dict(item) for item in (data.get("supplier_profiles") or [])
            if isinstance(item, dict)
        )

        self.window_geometry = data.get("window_geometry", "")
        self.window_state = data.get("window_state", "")
        self.splitter_sizes = [int(v) for v in data.get("splitter_sizes", []) if isinstance(v, int)]
        self.column_overrides = {
            path: {int(index): role for index, name in mapping.items() if (role := _role(name))}
            for path, mapping in (data.get("column_overrides") or {}).items()
        }
        self.update_check_auto = bool(data.get("update_check_auto", self.update_check_auto))
        self.update_download_auto = bool(data.get("update_download_auto", self.update_download_auto))
        self.update_show_changelog = bool(data.get("update_show_changelog", self.update_show_changelog))
        self.update_skip_version = str(data.get("update_skip_version", ""))
        self.update_remind_after = str(data.get("update_remind_after", ""))
        self.snapshots_enabled = bool(data.get("snapshots_enabled", self.snapshots_enabled))
        self.recent_payment_import = [
            p for p in data.get("recent_payment_import", []) if isinstance(p, str)]
        if levels := data.get("payment_levels"):
            values = [float(v) for v in levels if isinstance(v, (int, float)) and float(v) > 0]
            if len(values) == 3:
                self.payment_levels = sorted(values)
        self.payment_budget_warn = float(data.get("payment_budget_warn", self.payment_budget_warn))
        self.payment_import_reminder = bool(
            data.get("payment_import_reminder", self.payment_import_reminder))
        self.payment_import_seen = str(data.get("payment_import_seen", ""))

    def _as_dict(self) -> dict[str, Any]:
        return {
            "search": {
                "roles": sorted(r.value for r in self.search.roles),
                "weights": {r.value: w for r, w in self.search.weights.items() if w != DEFAULT_WEIGHTS.get(r)},
                "fuzzy_enabled": self.search.fuzzy_enabled,
                "fuzzy_threshold": self.search.fuzzy_threshold,
                "min_score": self.search.min_score,
            },
            "match": {
                "enforce_volume": self.match.enforce_volume,
                "volume_tolerance": self.match.volume_tolerance,
                "fuzzy_threshold": self.match.fuzzy_threshold,
                "auto_accept": self.match.auto_accept,
            },
            "fill_roles": [r.value for r in self.fill_roles],
            "overwrite_filled": self.overwrite_filled,
            "recent_source": self.recent_source,
            "recent_target": self.recent_target,
            "extra_sources": self.extra_sources,
            "recent_order_source": self.recent_order_source,
            "recent_order_target": self.recent_order_target,
            "order_sheets": self.order_sheets,
            "order_aliases": [alias.as_dict() for alias in self.order_aliases.items],
            "price_match": {
                "use_article": self.price_match.use_article,
                "use_ean": self.price_match.use_ean,
                "use_sku": self.price_match.use_sku,
                "use_name": self.price_match.use_name,
                "use_fuzzy": self.price_match.use_fuzzy,
                "ignore_case": self.price_match.ignore_case,
                "ignore_spaces": self.price_match.ignore_spaces,
                "ignore_symbols": self.price_match.ignore_symbols,
                "min_score": self.price_match.min_score,
                "separators": self.price_match.separators,
                "modifier_separators": self.price_match.modifier_separators,
            },
            "price_skip_unchanged": self.price_skip_unchanged,
            "recent_price_template": self.recent_price_template,
            "recent_price_supplier": self.recent_price_supplier,
            "price_sheets": self.price_sheets,
            "supplier_profiles": [p.as_dict() for p in self.supplier_profiles.items],
            "window_geometry": self.window_geometry,
            "window_state": self.window_state,
            "splitter_sizes": self.splitter_sizes,
            "column_overrides": {
                path: {str(index): role.value for index, role in mapping.items()}
                for path, mapping in self.column_overrides.items()
            },
            "update_check_auto": self.update_check_auto,
            "update_download_auto": self.update_download_auto,
            "update_show_changelog": self.update_show_changelog,
            "update_skip_version": self.update_skip_version,
            "update_remind_after": self.update_remind_after,
            "snapshots_enabled": self.snapshots_enabled,
            "recent_payment_import": self.recent_payment_import,
            "payment_levels": self.payment_levels,
            "payment_budget_warn": self.payment_budget_warn,
            "payment_import_reminder": self.payment_import_reminder,
            "payment_import_seen": self.payment_import_seen,
        }

    def remember_payment_import(self, path: str) -> None:
        _remember(self.recent_payment_import, path)


def _remember(recent: list[str], path: str) -> None:
    if path in recent:
        recent.remove(path)
    recent.insert(0, path)
    del recent[MAX_RECENT:]


def _role(value: object) -> FieldRole | None:
    """Роль из чего угодно: самой роли, её значения или строки из файла настроек.

    Готовую роль нужно проверять первой: начиная с Python 3.11 `str()` от
    члена перечисления даёт «FieldRole.PRICE», а не «price», и разбор такого
    текста молча вернул бы None — роль потерялась бы без единого сообщения.
    """
    if isinstance(value, FieldRole):
        return value
    try:
        return FieldRole(str(value))
    except ValueError:
        return None


def default_search_roles() -> set[FieldRole]:
    return set(DEFAULT_SEARCH_ROLES)
