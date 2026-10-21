@echo off
setlocal
chcp 65001 >nul
where git >nul 2>&1
if errorlevel 1 (
  echo Git is required. Install Git from https://git-scm.com/downloads/win
  pause
  exit /b 1
)
set "H3_DEST=%USERPROFILE%\Downloads\h3-batch-worker-production"
if exist "%H3_DEST%" (
  echo Destination already exists: "%H3_DEST%"
  echo Nothing was overwritten. Run setup-production.cmd inside it.
  pause
  exit /b 1
)
echo Downloading the production version. Sign in to GitHub if requested.
git clone --branch main --single-branch https://github.com/MasAshlia/h3-batch-worker.git "%H3_DEST%"
if errorlevel 1 (
  echo Download failed. Check network access and GitHub permissions.
  pause
  exit /b 1
)
call "%H3_DEST%\setup-production.cmd"
exit /b %ERRORLEVEL%
