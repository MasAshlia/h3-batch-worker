@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv not found. Run setup-production.cmd first.
  pause
  exit /b 1
)

if not exist "private\config.windows.yaml" (
  echo ERROR: private\config.windows.yaml not found. Run setup-production.cmd first.
  pause
  exit /b 1
)

if not exist "private\inbox" mkdir "private\inbox"

echo H3 continuous worker started.
echo Put each completed episode in private\inbox as an EP33.ready folder.
echo Each shot must be a TXT file whose first line starts with SHOT 33-16.
echo Legacy .ready.yaml and .ready.json episode files are also supported.
echo Press Ctrl+C to stop after the current operation.
echo.

".venv\Scripts\python.exe" -m h3_worker.cli watch "private\inbox" --real --config "private\config.windows.yaml"
set worker_exit=%ERRORLEVEL%

echo.
echo H3 continuous worker stopped with exit code %worker_exit%.
pause
exit /b %worker_exit%
