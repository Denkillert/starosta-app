@echo off
cd /d "%~dp0"

echo ========================================
echo   Запуск проекта "Староста"
echo ========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo ОШИБКА: виртуальное окружение .venv не найдено.
    echo Сначала создайте его: python -m venv .venv
    echo А затем установите зависимости: .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist "tools\cloudflared.exe" (
    echo ОШИБКА: tools\cloudflared.exe не найден.
    echo Скачайте его с: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
    pause
    exit /b 1
)

echo [1/3] Запуск веб-сервера...
start "Starosta Web" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"

timeout /t 3 /nobreak >nul

echo [2/3] Запуск Cloudflare туннеля (HTTPS)...
start "Starosta Tunnel" cmd /k "tools\cloudflared.exe tunnel --url http://localhost:8000"

timeout /t 8 /nobreak >nul

echo [3/3] Запуск Telegram-бота...
start "Starosta Bot" cmd /k ".venv\Scripts\python.exe -m bot.main"

echo.
echo ========================================
echo   Все процессы запущены
echo ========================================
echo.
echo ВАЖНО - выполните эти шаги вручную:
echo.
echo 1. Посмотрите в окно "Starosta Tunnel".
echo    Подождите строку вида:
echo      https://random-word.trycloudflare.com
echo    Скопируйте этот HTTPS URL.
echo.
echo 2. Откройте файл .env и установите:
echo      WEB_BASE_URL=https://random-word.trycloudflare.com
echo      DEV_MODE=false
echo      SESSION_SECRET=любая_длинная_случайная_строка_32_символа
echo.
echo 3. Перезапустите бота:
echo    - Закройте окно "Starosta Bot"
echo    - Запустите этот bat снова (или запустите бота вручную)
echo.
echo 4. В BotFather: /setmenubutton -^> выберите бота -^> вставьте HTTPS URL
echo.
echo Для остановки: закройте все три окна "Starosta".
echo.
pause