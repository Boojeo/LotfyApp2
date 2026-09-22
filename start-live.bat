@echo off
REM Double-click to run the trade manager on the LIVE account (REAL MONEY).
REM Restarts itself if it crashes. Close the window or press Ctrl+C to stop.
title Trade manager - LIVE
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
  echo No .venv folder here. Set it up once with:
  echo     py -m venv .venv
  echo     .venv\Scripts\activate
  echo     pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"

echo.
echo ##########################################
echo #  LIVE ACCOUNT - THIS IS REAL MONEY     #
echo ##########################################
echo.
echo Close this window now if you did not mean to.
echo Starting in 10 seconds...
timeout /t 10

:run
echo.
echo ==========================================
echo   LIVE account - REAL MONEY
echo   %date% %time%
echo ==========================================
echo.
python -m tmbot --env live --config config.yaml run
if "%ERRORLEVEL%"=="0" (
  echo.
  echo Stopped normally.
  pause
  exit /b 0
)
echo.
echo ------------------------------------------
echo   Stopped unexpectedly ^(code %ERRORLEVEL%^).
echo   Restarting in 15 seconds. Ctrl+C to give up.
echo   Your stop and target are still live at the
echo   broker while this is down.
echo ------------------------------------------
timeout /t 15
goto run
