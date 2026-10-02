@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
py -3.12 -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>&1
if not errorlevel 1 (
    py -3.12 "%~dp0scripts\windows\test_project.py"
    goto result
)
python -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>&1
if not errorlevel 1 (
    python "%~dp0scripts\windows\test_project.py"
    goto result
)
echo Python 3.12+ is required. Install Python from https://www.python.org/downloads/windows/
echo Enable the Python launcher or Add Python to PATH, then run this file again.
pause
exit /b 1
:result
set "H3_EXIT=%ERRORLEVEL%"
if not "%H3_EXIT%"=="0" echo H3 TEST FAILED. See the output above.
pause
exit /b %H3_EXIT%
