"""Памятка о сроках обязательной маркировки: данные и кнопка в заголовке."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.marking import deadlines
from app.core.marking.models import GROUPS


# --- данные -------------------------------------------------------------------------

def test_в_памятке_все_группы_программы_и_только_они():
    # Порядок свой — основной ассортимент первым, — а состав и названия те же.
    assert {item.code: item.title for item in deadlines.DEADLINES} == {
        group.code: group.title for group in GROUPS}
    assert len(deadlines.DEADLINES) == len(GROUPS)


def test_у_каждой_группы_сказано_что_действует_сейчас():
    assert all(item.status.strip() for item in deadlines.DEADLINES)


def test_ближайшие_даты_берутся_в_окне_и_по_возрастанию():
    found = deadlines.upcoming(date(2026, 10, 15), within=60)

    assert [(group.code, item.day) for group, item in found] == [
        ("milk", date(2026, 11, 1)),
        ("lp", date(2026, 12, 1)),
        ("cosmetics", date(2026, 12, 1)),
    ]


def test_прошедшие_и_далёкие_даты_не_напоминаются():
    assert deadlines.upcoming(date(2027, 6, 1)) == []
    assert deadlines.upcoming(date(2026, 8, 20), within=5) == []


def test_дата_сегодня_ещё_впереди():
    found = deadlines.upcoming(date(2026, 11, 1), within=0)

    assert [(group.code, item.day) for group, item in found] == [
        ("milk", date(2026, 11, 1))]


def test_подпись_кнопки_называет_ближайшее():
    assert deadlines.soon_caption(date(2026, 10, 15)) == (
        "скоро 1 ноября: Молочная продукция")
    assert deadlines.soon_caption(date(2027, 6, 1)) == ""


def test_таблица_устаревает_через_четыре_месяца():
    assert not deadlines.stale(date(2026, 10, 15))
    assert not deadlines.stale(date(2027, 2, 1))
    assert deadlines.stale(date(2027, 3, 1))


def test_текст_памятки_отделяет_прошедшее_от_будущего():
    text = deadlines.as_html(date(2026, 10, 15), muted="#111111", accent="#222222")

    assert "Молочная продукция" in text and "Табачная продукция" in text
    assert "<b>01.11.2026</b>" in text and "через 17 дн." in text
    assert "color:#222222" in text and "color:#111111" in text
    assert "не официальный документ" in text
    assert "устарел" not in text


def test_устаревшая_таблица_предупреждает_в_тексте():
    assert "могла устареть" in deadlines.as_html(date(2027, 6, 1))


def test_спорные_места_названы_в_тексте_а_не_скрыты():
    text = deadlines.as_html(date(2026, 10, 15))

    assert "источники расходятся" in text


# --- кнопка и памятка на странице ----------------------------------------------------

@pytest.fixture(scope="module")
def application():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _page(tmp_path, monkeypatch, today):
    from app.core.settings import AppSettings
    from app.ui import marking_page

    class Fixed(date):
        @classmethod
        def today(cls):
            return today

    monkeypatch.setattr(deadlines, "date", Fixed)
    return marking_page.MarkingPage(AppSettings(_path=str(tmp_path / "s.json")),
                                    lambda *_: None)


def test_памятка_свёрнута_пока_её_не_откроют(application, tmp_path, monkeypatch):
    page = _page(tmp_path, monkeypatch, date(2026, 10, 15))

    assert page.deadlines_card.isHidden()

    page.deadlines_button.setChecked(True)

    assert not page.deadlines_card.isHidden()
    assert "Лёгкой промышленности" in page.deadlines_text.toPlainText() \
        or "лёгкой промышленности" in page.deadlines_text.toPlainText()

    page.deadlines_button.setChecked(False)
    assert page.deadlines_card.isHidden()


def test_близкая_дата_видна_на_самой_кнопке(application, tmp_path, monkeypatch):
    from app.ui.theme import Palette

    page = _page(tmp_path, monkeypatch, date(2026, 10, 15))

    assert page.deadlines_button.text() == "Сроки маркировки · 1 ноября"
    assert Palette.WARNING in page.deadlines_button.styleSheet()
    # Что за дата — в подсказке, а не на кнопке.
    assert "01.11.2026 — Молочная продукция" in page.deadlines_button.toolTip()


def test_без_близких_дат_кнопка_спокойная(application, tmp_path, monkeypatch):
    page = _page(tmp_path, monkeypatch, date(2027, 6, 1))

    assert page.deadlines_button.text() == "Сроки маркировки"
    from app.ui.theme import Palette

    assert Palette.WARNING not in page.deadlines_button.styleSheet()
