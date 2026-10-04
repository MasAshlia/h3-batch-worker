@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv\Scripts\python.exe not found
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m h3_worker.ui_server --config private\config.windows.yaml
if errorlevel 1 pause
