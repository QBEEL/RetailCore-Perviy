"""Общие настройки тестов."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_appdata(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Папка данных приложения — временная в каждом тесте.

    `AppSettings()` и базы по умолчанию смотрят в настоящий %APPDATA%\\RetailCore.
    Тест, создавший главное окно, сохранял туда пустые настройки, и на машине
    разработчика каждый прогон стирал исключения переноса и недавние файлы.
    """
    folder = tmp_path_factory.mktemp("appdata")
    monkeypatch.setenv("APPDATA", str(folder))
    monkeypatch.setenv("HOME", str(folder))
    return folder
