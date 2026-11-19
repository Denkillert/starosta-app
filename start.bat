@echo off
cd /d "%~dp0"

echo ========================================
echo   Starosta launcher (Secure Local Dev)
echo ========================================
echo.

rem --- Check virtual environment ---
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found.
    echo Run: python -m venv .venv
    pause
    exit /b 1
)

rem --- Check cloudflared ---
if not exist "tools\cloudflared.exe" (
    echo ERROR: tools\cloudflared.exe not found.
    pause
    exit /b 1
)

rem --- Clean old log ---
del /q "%TEMP%\cloudflared.log" 2>nul

echo [1/4] Starting Cloudflare tunnel...
rem Запускаем cloudflared БЕЗ --no-tls-verify для безопасности.
rem Если здесь будет ошибка сертификата, проверьте настройки антивируса/сети.
start /b "" cmd /c "tools\cloudflared.exe tunnel --url http://127.0.0.1:8000 > "%TEMP%\cloudflared.log" 2>&1"

echo     Waiting for HTTPS URL (up to 20 sec)...
set HTTPS_URL=

rem Ждём появления URL. Регулярное выражение строго ограничено доменом trycloudflare.com
for /L %%i in (1,1,20) do (
    timeout /t 1 /nobreak >nul
    if exist "%TEMP%\cloudflared.log" (
        for /f "delims=" %%u in ('powershell -NoProfile -Command "$m = Select-String -Path '%TEMP:\=\\%\\cloudflared.log' -Pattern 'https://[a-z0-9\-]{1,50}\.trycloudflare\.com' | Select-Object -First 1; if ($m) { $m.Matches.Value }"') do (
            if not defined HTTPS_URL set HTTPS_URL=%%u
        )
        if defined HTTPS_URL goto :url_found
    )
)

echo ERROR: Could not get URL from cloudflared.
echo Check %TEMP%\cloudflared.log for details.
echo TIP: If you see TLS certificate errors, update your OS root certificates or check antivirus HTTPS scanning settings.
pause
exit /b 1

:url_found
echo     Got URL: %HTTPS_URL%

echo [2/4] Updating .env...
rem Безопасное обновление .env через PowerShell
powershell -NoProfile -Command "$f = '.env'; $c = Get-Content $f -Raw; $n = $c -replace '(?m)^WEB_BASE_URL=.*$', 'WEB_BASE_URL=%HTTPS_URL%'; if ($n -ne $c) { [System.IO.File]::WriteAllText($f, $n, [System.Text.Encoding]::UTF8) }; Write-Host '    .env updated.'"
echo.

echo [3/4] Starting web server (Localhost only)...
rem ИСПРАВЛЕНО: Привязка только к 127.0.0.1, а не к 0.0.0.0
start "Starosta Web" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

timeout /t 2 /nobreak >nul

echo [4/4] Starting bot...
start "Starosta Bot" cmd /k ".venv\Scripts\python.exe -m bot.main"

echo.
echo ========================================
echo   All processes started securely
echo ========================================
echo.
echo URL: %HTTPS_URL%
echo.
echo The bot will automatically update the Telegram menu button.
echo Web server is bound to 127.0.0.1 (local access only via tunnel).
echo To stop: close all three "Starosta" windows.
echo.
pause