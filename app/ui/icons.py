"""Иконки qtawesome с единой палитрой и запасным вариантом без библиотеки."""
from __future__ import annotations

from PySide6.QtGui import QIcon

from .resources import asset
from .theme import Palette

try:
    import qtawesome
except ImportError:  # приложение остаётся работоспособным без иконочного шрифта
    qtawesome = None

NAMES = {
    "match": "mdi6.compare-horizontal",
    "catalog": "mdi6.database-search",
    "order": "mdi6.clipboard-arrow-right-outline",
    "settings": "mdi6.tune-variant",
    "open": "mdi6.folder-open-outline",
    "run": "mdi6.play-circle-outline",
    "save": "mdi6.content-save-outline",
    "search": "mdi6.magnify",
    "clear": "mdi6.close-circle-outline",
    "link": "mdi6.link-variant",
    "unlink": "mdi6.link-variant-off",
    "check": "mdi6.check-circle",
    "warning": "mdi6.alert-circle",
    "error": "mdi6.close-circle",
    "info": "mdi6.information",
    "question": "mdi6.help-circle",
    "copy": "mdi6.content-copy",
    "columns": "mdi6.view-column-outline",
    "filter": "mdi6.filter-variant",
    "card": "mdi6.card-text-outline",
    "recent": "mdi6.history",
    "file": "mdi6.file-excel-outline",
    "reset": "mdi6.backup-restore",
    "chevron": "mdi6.chevron-right",
    "export": "mdi6.file-export-outline",
    "aliases": "mdi6.bookmark-multiple-outline",
    "star": "mdi6.star-outline",
    "update": "mdi6.arrow-up-bold-circle-outline",
    "history": "mdi6.database-clock-outline",
    "compare": "mdi6.compare",
    "trash": "mdi6.trash-can-outline",
    "folder": "mdi6.folder-cog-outline",
}


def icon(name: str, color: str = Palette.TEXT_MUTED, size: int | None = None) -> QIcon:
    if qtawesome is None:
        return QIcon()
    options = {"color": color, "color_active": color}
    if size:
        options["scale_factor"] = size / 16
    return qtawesome.icon(NAMES.get(name, name), **options)


def available() -> bool:
    return qtawesome is not None


def app_icon() -> QIcon:
    """Иконка приложения: окно, панель задач, файл .exe."""
    path = asset("app.ico")
    return QIcon(str(path)) if path.exists() else icon("match", Palette.PRIMARY)
