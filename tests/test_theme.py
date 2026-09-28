"""Тесты темы: светлая палитра не зависит от оформления системы."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QPalette  # noqa: E402

from app.ui.theme import Palette, light_palette  # noqa: E402


def test_light_palette_uses_theme_tokens() -> None:
    """Фон вкладок и списков — из токенов темы, а не из системной палитры.

    На Mac с тёмным оформлением системная палитра тёмная, и страницы вкладок
    «Календарь» и «Таблица», которые таблица стилей не закрашивает, чернели.
    """
    palette = light_palette()
    assert palette.color(QPalette.ColorRole.Window).name() == Palette.BG
    assert palette.color(QPalette.ColorRole.Base).name() == Palette.SURFACE
    assert palette.color(QPalette.ColorRole.Text).name() == Palette.TEXT
    assert palette.color(QPalette.ColorRole.WindowText).name() == Palette.TEXT
