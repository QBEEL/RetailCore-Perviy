@echo off
title RetailCore
cd /d "%~dp0"

set "PYDIR=%~dp0.venv\Scripts"

if not exist "%PYDIR%\python.exe" goto setup
goto launch

:setup
echo.
echo  Первый запуск: создаю окружение и устанавливаю библиотеки.
echo  Это займет несколько минут, скачается около 250 МБ.
echo.
python -m venv "%~dp0.venv"
if errorlevel 1 goto nopython
"%PYDIR%\python.exe" -m pip install --upgrade pip --quiet
"%PYDIR%\python.exe" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto nodeps
echo.
echo  Готово. Запускаю приложение.

:launch
if exist "%PYDIR%\pythonw.exe" goto quiet
start "" "%PYDIR%\python.exe" run.py
goto end

:quiet
start "" "%PYDIR%\pythonw.exe" run.py
goto end

:nopython
echo.
echo  Не удалось создать окружение.
echo  Установите Python 3.11 или новее с сайта python.org
echo  и обязательно отметьте пункт "Add Python to PATH".
echo.
pause
exit /b 1

:nodeps
echo.
echo  Не удалось установить библиотеки.
echo  Проверьте подключение к интернету и запустите файл заново.
echo.
pause
exit /b 1

:end