@echo off
title Moment-CUT - Connect Rumble
cd /d "%~dp0"
echo =======================================================
echo   Moment-CUT - Rumble Account Connection Helper
echo =======================================================
echo.
echo [1] Open browser to log in to Rumble (Edge / Chrome)
echo [2] Enter Rumble session cookie (u_s) directly
echo.
set /p opt="Choose an option [1 or 2, default 1]: "

if "%opt%"=="2" (
    echo.
    echo To get your u_s cookie:
    echo 1. Log in to rumble.com in your browser.
    echo 2. Press F12, open Application ^> Cookies ^> rumble.com
    echo 3. Copy the Value of the cookie named 'u_s'
    echo.
    set /p cval="Paste the 'u_s' cookie value here: "
    set /p uname="Enter your Rumble channel/username: "
    .\.venv\Scripts\python.exe -c "from studio import rumble; res = rumble.save_cookie_session('%cval%', '%uname%'); print('Connected:', res)"
    echo.
    echo Done! Check your Moment-CUT settings now.
    pause
    exit /b
)

echo.
echo Opening browser window to sign in to Rumble...
echo Please log in and complete any verification in the browser.
echo.
if exist .venv\Scripts\python.exe (
    .\.venv\Scripts\python.exe -u -m studio.rumble
) else (
    python -u -m studio.rumble
)
echo.
echo Done! You can return to PeakCut now.
pause
