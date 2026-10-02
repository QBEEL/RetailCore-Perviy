"""Сборка `.exe`: модули, которые PyInstaller не находит сам.

`win32timezone` pywin32 подгружает динамически при переводе дат сертификата из
COM. Без него в собранном `.exe` пропадают личные сертификаты для «Честного
знака», а из `start.bat` всё работает — там модуль лежит в окружении.
"""
from pathlib import Path

SPEC = Path(__file__).resolve().parents[1] / "RetailCore.spec"


def test_exe_берёт_win32timezone_для_сертификатов():
    assert '"win32timezone"' in SPEC.read_text(encoding="utf-8")
