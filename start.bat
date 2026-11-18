@echo off
rem ============================================================
rem  Запуск проекта "Староста"
rem  ВАЖНО: этот файл должен быть сохранён в кодировке CP866
rem  (Cyrillic DOS). НЕ сохраняйте его в UTF-8 и НЕ добавляйте
rem  chcp 65001 - это ломает разбор файла cmd.exe.
rem ============================================================
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

echo [1/2] Запуск веб-сервера...
start "Starosta Web" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"

timeout /t 4 /nobreak >nul

echo [2/2] Запуск бота...
start "Starosta Bot" cmd /k ".venv\Scripts\python.exe -m bot.main"

echo.
echo Готово. Открылись два окна:
echo   "Starosta Web" - приложение, http://127.0.0.1:8000
echo                    (там же печатается IP для телефона)
echo   "Starosta Bot" - Telegram-бот
echo.
echo Для остановки закройте оба окна "Starosta".
echo.
pause