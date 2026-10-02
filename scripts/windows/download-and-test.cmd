@echo off
setlocal
chcp 65001 >nul
where git >nul 2>&1
if errorlevel 1 (
    echo Git is required. Install Git from https://git-scm.com/downloads/win
    echo Or download the repository ZIP from GitHub and double-click test-windows.cmd.
    pause
    exit /b 1
)
set "H3_DEST=%USERPROFILE%\Downloads\h3-batch-worker"
if exist "%H3_DEST%" (
    echo Destination already exists: "%H3_DEST%"
    echo Nothing was overwritten. Run test-windows.cmd inside it, or rename the folder first.
    pause
    exit /b 1
)
echo Downloading the private repository. Sign in to GitHub if Git asks you to.
git clone --branch main --single-branch https://github.com/MasAshlia/h3-batch-worker.git "%H3_DEST%"
if errorlevel 1 (
    echo Download failed. Check your network and GitHub repository access.
    pause
    exit /b 1
)
call "%H3_DEST%\test-windows.cmd"
exit /b %ERRORLEVEL%
