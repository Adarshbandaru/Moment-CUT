@echo off
title Moment-CUT
cd /d "%~dp0"

echo =======================================================
echo                 MOMENT - C U T
echo       AI Clip Maker & Auto-Poster Studio
echo =======================================================
echo.

:: Detect Python executable
set "PY_BIN="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_BIN=%~dp0.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if %errorlevel% equ 0 (
        set "PY_BIN=python"
    ) else (
        echo [ERROR] Python not found! Please install Python or setup .venv.
        pause
        exit /b 1
    )
)

echo Starting Moment-CUT Studio server...
echo.

:: Launch browser in background after 2 seconds
start /b cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8765"

:: Run the server
"%PY_BIN%" -m studio.server

pause
