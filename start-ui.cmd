@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv\Scripts\python.exe not found. Run setup-production.cmd first.
  pause
  exit /b 1
)
if not exist "private\config.windows.yaml" (
  echo ERROR: private\config.windows.yaml not found. Run setup-production.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m h3_worker.ui_server --config private\config.windows.yaml
if errorlevel 1 pause
