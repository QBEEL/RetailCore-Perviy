@echo off
chcp 65001 >nul
title Проверка СУЗ
cd /d "%~dp0"

set "PYDIR=%~dp0.venv\Scripts"
set "PYTHONIOENCODING=utf-8"

if not exist "%PYDIR%\python.exe" goto noenv

echo.
echo  Спрашиваем СУЗ, какие методы у неё есть.
echo  Ничего не заказывается и не расходуется - только чтение.
echo.

"%PYDIR%\python.exe" "%~dp0tools\suz_probe.py"

echo.
echo  Готово. Пришлите файл "Ответ СУЗ.txt" из этой папки.
echo.
pause
exit /b 0

:noenv
echo.
echo  Окружение .venv не найдено.
echo  Запустите сначала start.bat - он его создаст, - и повторите.
echo.
pause
exit /b 1
