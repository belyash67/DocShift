@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title DocShift (без установки)
echo DocShift - запуск без Flask, pip и прав администратора
echo.
py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>nul
if not errorlevel 1 (
  py -3 app.py
  goto finished
)
python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>nul
if not errorlevel 1 (
  python app.py
  goto finished
)
echo ОШИБКА: Python 3.10+ не найден. Если Python в VS Code работает, запусти app.py через F5.
:finished
echo.
echo Если сайт не открылся, смотри адрес и ошибки выше.
pause
