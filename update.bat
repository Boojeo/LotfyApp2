@echo off
REM One-click update for Windows. Double-click this file.
REM Your .env and config.yaml are never touched by an update, and are backed
REM up here anyway before anything else happens.
setlocal
cd /d "%~dp0"

echo ==========================================
echo   Updating the trade manager
echo ==========================================
echo.

where git >nul 2>nul
if errorlevel 1 (
  echo Git is not installed, so this folder cannot update itself.
  echo Install it from https://git-scm.com/download/win, then run this again.
  echo.
  pause
  exit /b 1
)

if not exist ".git" (
  echo This folder was downloaded as a ZIP rather than set up with Git,
  echo so it has no way to fetch updates. Ask for the one-time Git setup.
  echo.
  pause
  exit /b 1
)

echo [1/4] Backing up your settings...
if not exist "settings-backup" mkdir "settings-backup"
if exist ".env" copy /y ".env" "settings-backup\" >nul
if exist "config.yaml" copy /y "config.yaml" "settings-backup\" >nul
echo       copied to settings-backup\
echo.

echo [2/4] Downloading the latest version...
git pull --ff-only
if errorlevel 1 (
  echo.
  echo Update failed. Nothing was changed and your settings are safe
  echo in settings-backup\. Send the message above and it can be sorted out.
  echo.
  pause
  exit /b 1
)
echo.

echo [3/4] Installing any new parts...
if not exist ".venv\Scripts\activate.bat" (
  echo No .venv folder found. Create one first with:  py -m venv .venv
  echo.
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo Some parts failed to install. Charts may not work, but the bot will
  echo still run and send the full text plan.
  echo.
)
echo.

echo [4/4] Checking your settings still load...
echo.
python -m tmbot --env demo --config config.yaml check
echo.
echo ==========================================
echo   Update finished. Start the bot with:
echo   py -m tmbot --env demo --config config.yaml run
echo ==========================================
echo.
pause
