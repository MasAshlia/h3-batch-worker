@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "H3_PYTHON="
py -3.12 -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>&1
if not errorlevel 1 set "H3_PYTHON=py -3.12"
if not defined H3_PYTHON (
  python -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>&1
  if not errorlevel 1 set "H3_PYTHON=python"
)
if not defined H3_PYTHON (
  echo ERROR: Python 3.12+ is required. Install it from https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating project environment...
  %H3_PYTHON% -m venv .venv
  if errorlevel 1 goto failed
)

echo Installing H3 production worker...
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto failed

if not exist "private" mkdir "private"
if not exist "private\inbox" mkdir "private\inbox"
if not exist "private\config.windows.yaml" copy "production\config.windows.yaml" "private\config.windows.yaml" >nul
if not exist "private\production_h3_api.json" copy "production\production_h3_api.json" "private\production_h3_api.json" >nul
if not exist "private\asset_manifest.json" copy "production\asset_manifest.json" "private\asset_manifest.json" >nul

echo.
echo H3 production worker is installed.
echo 1. Edit private\config.windows.yaml if this PC uses different ComfyUI paths.
echo 2. Put reference images in the configured ComfyUI input folder.
echo 3. Add exact overrides to private\asset_manifest.json only when automatic matching is ambiguous.
echo 4. Start ComfyUI, then double-click start-ui.cmd.
pause
exit /b 0

:failed
echo ERROR: Production setup failed. No existing private configuration was overwritten.
pause
exit /b 1
