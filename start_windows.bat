@echo off
setlocal
cd /d "%~dp0"
title DocShift
echo Starting DocShift...
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
echo ERROR: Python 3.10 or newer is required.
echo If Python works in VS Code, open app.py and use Run Python File.
:finished
echo.
echo If the site did not open, check the address and errors above.
pause
