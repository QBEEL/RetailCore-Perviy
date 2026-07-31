"""Тесты настроек: разметка колонок и надёжность записи файла."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.models import FieldRole
from app.core.settings import AppSettings, _role


@pytest.fixture
def settings(tmp_path):
    return AppSettings(_path=str(tmp_path / "settings.json"))


# --- приведение роли ----------------------------------------------------------

def test_роль_узнаётся_и_в_виде_самой_роли():
    """С Python 3.11 str(FieldRole.PRICE) даёт «FieldRole.PRICE», а не «price»."""
    assert _role(FieldRole.PRICE) is FieldRole.PRICE
    assert _role("price") is FieldRole.PRICE
    assert _role("такой роли нет") is None
    assert _role(None) is None


def test_разметка_принимает_строку_из_выпадающего_списка(settings):
    """Qt возвращает из currentData() обычную строку: FieldRole унаследован от str."""
    settings.set_overrides("C:/прайс.xlsx", {4: "price", 7: "other"})
    saved = settings.overrides_for("C:/прайс.xlsx")
    assert saved == {4: FieldRole.PRICE, 7: FieldRole.OTHER}
    assert all(isinstance(role, FieldRole) for role in saved.values())


def test_разметка_принимает_и_саму_роль(settings):
    settings.set_overrides("C:/прайс.xlsx", {4: FieldRole.PRICE})
    assert settings.overrides_for("C:/прайс.xlsx") == {4: FieldRole.PRICE}


def test_неизвестная_роль_отбрасывается_а_не_ломает_сохранение(settings):
    settings.set_overrides("C:/прайс.xlsx", {4: "price", 5: "выдумка"})
    assert settings.overrides_for("C:/прайс.xlsx") == {4: FieldRole.PRICE}
    settings.save()  # не должно бросать


def test_пустая_разметка_убирает_запись_о_файле(settings):
    settings.set_overrides("C:/прайс.xlsx", {4: FieldRole.PRICE})
    settings.set_overrides("C:/прайс.xlsx", {})
    assert settings.overrides_for("C:/прайс.xlsx") == {}


def test_разметка_переживает_перезапись_и_чтение(settings):
    settings.set_overrides("C:/прайс.xlsx", {4: "price"})
    settings.save()
    assert AppSettings.load(settings._path).overrides_for("C:/прайс.xlsx") == {4: FieldRole.PRICE}


# --- запись файла -------------------------------------------------------------

def test_сбой_сборки_не_обнуляет_настройки(settings, monkeypatch):
    """Файл открывался на запись до сборки данных, и ошибка оставляла его пустым."""
    settings.recent_source = ["C:/каталог.xlsx"]
    settings.save()
    before = Path(settings._path).read_text(encoding="utf-8")

    def explode(self):
        raise RuntimeError("что-то пошло не так при сборке")

    monkeypatch.setattr(AppSettings, "_as_dict", explode)
    with pytest.raises(RuntimeError):
        settings.save()

    after = Path(settings._path).read_text(encoding="utf-8")
    assert after == before, "прежние настройки потеряны"
    assert json.loads(after)["recent_source"] == ["C:/каталог.xlsx"]


def test_временный_файл_за_собой_не_остаётся(settings):
    settings.save()
    assert not Path(f"{settings._path}.tmp").exists()


def test_пустой_файл_настроек_не_мешает_запуску(tmp_path):
    """После прежнего сбоя settings.json оставался нулевого размера."""
    path = tmp_path / "settings.json"
    path.write_text("", encoding="utf-8")
    loaded = AppSettings.load(str(path))
    assert loaded.recent_source == []
    loaded.save()
    assert json.loads(path.read_text(encoding="utf-8"))
