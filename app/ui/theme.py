"""Дизайн-токены и таблица стилей. Светлая тема, единая для всего приложения."""
from __future__ import annotations

from PySide6.QtGui import QColor

from .resources import asset_url

# QSS требует прямые слеши в url() даже на Windows; кавычки обязательны, иначе
# путь со спецсимволами (например «&» в имени папки) ломает разбор правила.
_CHECK_ICON = asset_url("check.svg")
_CHEVRON_UP = asset_url("chevron-up.svg")
_CHEVRON_DOWN = asset_url("chevron-down.svg")


class Palette:
    PRIMARY = "#2563eb"
    PRIMARY_HOVER = "#1d4ed8"
    PRIMARY_PRESSED = "#1e40af"
    PRIMARY_SOFT = "#eff6ff"

    SUCCESS = "#16a34a"
    SUCCESS_SOFT = "#f0fdf4"
    WARNING = "#d97706"
    WARNING_SOFT = "#fffbeb"
    DANGER = "#dc2626"
    DANGER_SOFT = "#fef2f2"
    INFO = "#0891b2"
    INFO_SOFT = "#ecfeff"

    BG = "#f5f7fa"
    SURFACE = "#ffffff"
    SURFACE_ALT = "#fafbfc"
    BORDER = "#e2e8f0"
    BORDER_STRONG = "#cbd5e1"

    TEXT = "#0f172a"
    TEXT_MUTED = "#64748b"
    TEXT_FAINT = "#94a3b8"
    TEXT_ON_PRIMARY = "#ffffff"

    SELECTION = "#dbeafe"
    HIGHLIGHT = "#fde68a"


class Metrics:
    RADIUS = 10
    RADIUS_SM = 6
    RADIUS_LG = 14
    GAP = 12
    PAD = 16
    ROW_HEIGHT = 34
    SIDEBAR_WIDTH = 212


STATUS_COLORS: dict[str, tuple[str, str]] = {
    "matched": (Palette.SUCCESS, Palette.SUCCESS_SOFT),
    "review": (Palette.WARNING, Palette.WARNING_SOFT),
    "ambiguous": (Palette.INFO, Palette.INFO_SOFT),
    "manual": (Palette.PRIMARY, Palette.PRIMARY_SOFT),
    "not_found": (Palette.DANGER, Palette.DANGER_SOFT),
}


def score_color(score: float) -> QColor:
    """Цвет индикатора Match Score: от красного к зелёному."""
    if score >= 90:
        return QColor(Palette.SUCCESS)
    if score >= 75:
        return QColor("#65a30d")
    if score >= 55:
        return QColor(Palette.WARNING)
    return QColor(Palette.DANGER)


STYLESHEET = f"""
* {{
    font-family: "Segoe UI Variable Text", "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
    color: {Palette.TEXT};
}}

QWidget#Root, QMainWindow {{ background: {Palette.BG}; }}

QLabel#PageTitle {{ font-size: 22px; font-weight: 600; }}
QLabel#PageSubtitle {{ font-size: 13px; color: {Palette.TEXT_MUTED}; }}
QLabel#SectionTitle {{ font-size: 15px; font-weight: 600; }}
QLabel#Hint {{ color: {Palette.TEXT_MUTED}; font-size: 12px; }}
QLabel#Metric {{ font-size: 24px; font-weight: 600; }}
QLabel#MetricLabel {{ font-size: 12px; color: {Palette.TEXT_MUTED}; }}

QFrame#Card, QWidget#Card {{
    background: {Palette.SURFACE};
    border: 1px solid {Palette.BORDER};
    border-radius: {Metrics.RADIUS}px;
}}
QFrame#Divider {{ background: {Palette.BORDER}; max-height: 1px; border: none; }}

QWidget#Sidebar {{
    background: {Palette.SURFACE};
    border-right: 1px solid {Palette.BORDER};
}}
QLabel#Brand {{ font-size: 16px; font-weight: 700; }}
QLabel#BrandSub {{ font-size: 11px; color: {Palette.TEXT_FAINT}; }}

QPushButton#NavButton {{
    background: transparent;
    border: none;
    border-radius: {Metrics.RADIUS_SM}px;
    padding: 10px 12px;
    text-align: left;
    font-size: 13px;
    font-weight: 500;
    color: {Palette.TEXT_MUTED};
}}
QPushButton#NavButton:hover {{ background: {Palette.SURFACE_ALT}; color: {Palette.TEXT}; }}
QPushButton#NavButton:checked {{
    background: {Palette.PRIMARY_SOFT};
    color: {Palette.PRIMARY};
    font-weight: 600;
}}

QPushButton {{
    background: {Palette.SURFACE};
    border: 1px solid {Palette.BORDER_STRONG};
    border-radius: {Metrics.RADIUS_SM}px;
    padding: 7px 14px;
    font-weight: 500;
}}
QPushButton:hover {{ background: {Palette.SURFACE_ALT}; border-color: {Palette.TEXT_FAINT}; }}
QPushButton:pressed {{ background: {Palette.BORDER}; }}
QPushButton:disabled {{ color: {Palette.TEXT_FAINT}; background: {Palette.SURFACE_ALT}; }}

QPushButton#Primary {{
    background: {Palette.PRIMARY};
    border: 1px solid {Palette.PRIMARY};
    color: {Palette.TEXT_ON_PRIMARY};
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background: {Palette.PRIMARY_HOVER}; border-color: {Palette.PRIMARY_HOVER}; }}
QPushButton#Primary:pressed {{ background: {Palette.PRIMARY_PRESSED}; }}
QPushButton#Primary:disabled {{ background: {Palette.BORDER_STRONG}; border-color: {Palette.BORDER_STRONG}; color: {Palette.SURFACE}; }}

QPushButton#Success {{
    background: {Palette.SUCCESS}; border-color: {Palette.SUCCESS};
    color: {Palette.TEXT_ON_PRIMARY}; font-weight: 600;
}}
QPushButton#Success:hover {{ background: #15803d; border-color: #15803d; }}
QPushButton#Success:disabled {{ background: {Palette.BORDER_STRONG}; border-color: {Palette.BORDER_STRONG}; color: {Palette.SURFACE}; }}

QPushButton#Danger {{ color: {Palette.DANGER}; border-color: #fecaca; }}
QPushButton#Danger:hover {{ background: {Palette.DANGER_SOFT}; }}

QPushButton#Ghost {{ background: transparent; border: none; padding: 6px 8px; color: {Palette.TEXT_MUTED}; }}
QPushButton#Ghost:hover {{ background: {Palette.SURFACE_ALT}; color: {Palette.TEXT}; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {Palette.SURFACE};
    border: 1px solid {Palette.BORDER_STRONG};
    border-radius: {Metrics.RADIUS_SM}px;
    padding: 7px 10px;
    selection-background-color: {Palette.SELECTION};
    selection-color: {Palette.TEXT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {Palette.PRIMARY};
}}
QLineEdit:disabled {{ background: {Palette.SURFACE_ALT}; color: {Palette.TEXT_FAINT}; }}
QLineEdit#Path {{ font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; }}

QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {Palette.SURFACE};
    border: 1px solid {Palette.BORDER};
    border-radius: {Metrics.RADIUS_SM}px;
    selection-background-color: {Palette.PRIMARY_SOFT};
    selection-color: {Palette.TEXT};
    padding: 4px;
}}

QTableView {{
    background: {Palette.SURFACE};
    border: 1px solid {Palette.BORDER};
    border-radius: {Metrics.RADIUS}px;
    gridline-color: {Palette.BORDER};
    selection-background-color: {Palette.SELECTION};
    selection-color: {Palette.TEXT};
    alternate-background-color: {Palette.SURFACE_ALT};
}}
QTableView::item {{ padding: 6px 8px; border: none; }}
QTableView::item:hover {{ background: {Palette.PRIMARY_SOFT}; }}
QTableView::item:selected {{ background: {Palette.SELECTION}; color: {Palette.TEXT}; }}

QHeaderView::section {{
    background: {Palette.SURFACE_ALT};
    border: none;
    border-bottom: 1px solid {Palette.BORDER};
    border-right: 1px solid {Palette.BORDER};
    padding: 9px 8px;
    font-weight: 600;
    font-size: 12px;
    color: {Palette.TEXT_MUTED};
}}
QHeaderView::section:hover {{ background: {Palette.PRIMARY_SOFT}; color: {Palette.PRIMARY}; }}
QTableCornerButton::section {{ background: {Palette.SURFACE_ALT}; border: none; }}

QListWidget {{
    background: {Palette.SURFACE};
    border: 1px solid {Palette.BORDER};
    border-radius: {Metrics.RADIUS_SM}px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{ border-radius: {Metrics.RADIUS_SM}px; padding: 7px 9px; margin: 1px 0; }}
QListWidget::item:hover {{ background: {Palette.SURFACE_ALT}; }}
QListWidget::item:selected {{ background: {Palette.PRIMARY_SOFT}; color: {Palette.TEXT}; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {Palette.BORDER_STRONG}; border-radius: 5px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: {Palette.TEXT_FAINT}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {Palette.BORDER_STRONG}; border-radius: 5px; min-width: 32px; }}
QScrollBar::handle:horizontal:hover {{ background: {Palette.TEXT_FAINT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QProgressBar {{
    background: {Palette.BORDER};
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
}}
QProgressBar::chunk {{ background: {Palette.PRIMARY}; border-radius: 3px; }}

QCheckBox, QRadioButton {{ spacing: 8px; padding: 3px 0; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 17px; height: 17px;
    border: 1px solid {Palette.BORDER_STRONG};
    border-radius: 4px;
    background: {Palette.SURFACE};
}}
QCheckBox::indicator:hover {{ border-color: {Palette.PRIMARY}; }}
QCheckBox::indicator:checked {{
    background: {Palette.PRIMARY};
    border-color: {Palette.PRIMARY};
    image: url("{_CHECK_ICON}");
}}
QCheckBox::indicator:checked:hover {{ background: {Palette.PRIMARY_HOVER}; }}
QCheckBox::indicator:disabled {{ background: {Palette.SURFACE_ALT}; border-color: {Palette.BORDER}; }}

QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    width: 18px;
    border: none;
    background: transparent;
    subcontrol-origin: border;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-position: top right;
    image: url("{_CHEVRON_UP}");
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-position: bottom right;
    image: url("{_CHEVRON_DOWN}");
}}
QComboBox::down-arrow {{ image: url("{_CHEVRON_DOWN}"); width: 9px; height: 9px; }}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {Palette.PRIMARY_SOFT};
}}
QRadioButton::indicator {{ border-radius: 9px; }}
QRadioButton::indicator:checked {{ background: {Palette.PRIMARY}; border: 5px solid {Palette.SURFACE}; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QSplitter::handle:hover {{ background: {Palette.BORDER}; }}

QMenu {{
    background: {Palette.SURFACE};
    border: 1px solid {Palette.BORDER};
    border-radius: {Metrics.RADIUS_SM}px;
    padding: 5px;
}}
QMenu::item {{ padding: 7px 24px 7px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: {Palette.PRIMARY_SOFT}; color: {Palette.PRIMARY}; }}
QMenu::separator {{ height: 1px; background: {Palette.BORDER}; margin: 5px 8px; }}

QToolTip {{
    background: {Palette.TEXT};
    color: {Palette.SURFACE};
    border: none;
    border-radius: {Metrics.RADIUS_SM}px;
    padding: 6px 9px;
}}

QDialog {{ background: {Palette.BG}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

QTabWidget::pane {{ border: none; }}
QGroupBox {{
    background: {Palette.SURFACE};
    border: 1px solid {Palette.BORDER};
    border-radius: {Metrics.RADIUS}px;
    margin-top: 14px;
    padding: 14px;
    font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 5px; color: {Palette.TEXT_MUTED}; }}
"""
