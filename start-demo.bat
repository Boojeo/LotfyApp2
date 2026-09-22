@echo off
REM Double-click to run the trade manager on the DEMO account (practice money).
REM Restarts itself if it crashes. Close the window or press Ctrl+C to stop.
title Trade manager - DEMO
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

:run
echo.
echo ==========================================
echo   DEMO account - practice money
echo   %date% %time%
echo ==========================================
echo.
python -m tmbot --env demo --config config.yaml run
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
