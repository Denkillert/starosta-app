@echo off
cd /d "%~dp0"

echo ========================================
echo   Starosta launcher
echo ========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found.
    echo Run: python -m venv .venv
    pause
    exit /b 1
)

if not exist "tools\cloudflared.exe" (
    echo ERROR: tools\cloudflared.exe not found.
    echo Download cloudflared-windows-amd64.exe from:
    echo   https://github.com/cloudflare/cloudflared/releases/latest
    echo Save it as tools\cloudflared.exe
    pause
    exit /b 1
)

echo [1/3] Starting web server on port 8000...
start "Starosta Web" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"

timeout /t 3 /nobreak >nul

echo [2/3] Starting Cloudflare HTTPS tunnel...
start "Starosta Tunnel" cmd /k "tools\cloudflared.exe tunnel --url http://localhost:8000"

timeout /t 8 /nobreak >nul

echo [3/3] Starting Telegram bot...
start "Starosta Bot" cmd /k ".venv\Scripts\python.exe -m bot.main"

echo.
echo ========================================
echo   IMPORTANT - 4 steps to enable Mini App
echo ========================================
echo.
echo 1. Open the "Starosta Tunnel" window.
echo    Wait until you see a line like:
echo      https://random-words-trycloudflare.trycloudflare.com
echo    COPY that URL (right-click -^> Mark -^> Enter).
echo.
echo 2. Open .env in this folder and change:
echo      WEB_BASE_URL=https://random-words-trycloudflare.trycloudflare.com
echo    Save the file.
echo.
echo 3. Restart the bot:
echo    - Close the "Starosta Bot" window
echo    - Run: .venv\Scripts\python.exe -m bot.main
echo    (or just close all windows and run this bat again)
echo.
echo 4. In @BotFather:
echo      /setmenubutton
echo      -^> choose your bot
echo      -^> paste the SAME https:// URL
echo      -^> type button text: Open app
echo.
echo After step 4, open your bot in Telegram.
echo The menu button (left of input field) will show "Open app".
echo Tapping it opens the Mini App inside Telegram.
echo.
echo To stop: close all three "Starosta" windows.
echo.
pause