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
    _path: str = field(default_factory=settings_path, repr=False)

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
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump(self._as_dict(), handle, ensure_ascii=False, indent=2)

    def remember_file(self, path: str, *, source: bool) -> None:
        _remember(self.recent_source if source else self.recent_target, path)

    def remember_order_file(self, path: str, *, source: bool) -> None:
        _remember(self.recent_order_source if source else self.recent_order_target, path)

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
        if overrides:
            self.column_overrides[path] = overrides
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
        }


def _remember(recent: list[str], path: str) -> None:
    if path in recent:
        recent.remove(path)
    recent.insert(0, path)
    del recent[MAX_RECENT:]


def _role(name: object) -> FieldRole | None:
    try:
        return FieldRole(str(name))
    except ValueError:
        return None


def default_search_roles() -> set[FieldRole]:
    return set(DEFAULT_SEARCH_ROLES)
