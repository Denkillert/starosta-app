@echo off
chcp 65001 >nul
cd /d %~dp0

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
start "StarostaWeb" /b cmd /c ".venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"

timeout /t 4 /nobreak >nul

echo [2/2] Запуск бота...
start "StarostaBot" /b cmd /c ".venv\Scripts\python.exe -m bot.main"

echo.
echo Готово. Приложение доступно:
echo   http://192.168.0.109:8000  (с телефона в той же Wi-Fi)
echo   http://127.0.0.1:8000        (с этого компьютера)
echo.
echo Кнопка "Открыть в браузере" в боте откроет приложение на телефоне.
echo Для остановки закройте оба консольных окна.
echo.
pause