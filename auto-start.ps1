# Настройка кодировки для корректного вывода кириллицы
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$projectRoot = $PSScriptRoot
$envPath = Join-Path $projectRoot ".env"
$cloudflaredPath = Join-Path $projectRoot "tools\cloudflared.exe"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Автозапуск Староста (с обновлением URL)" -ForegroundColor Cyan
Write-Host "========================================"

# 1. Проверка наличия файлов
if (-not (Test-Path $venvPython)) {
    Write-Host "ОШИБКА: Виртуальное окружение .venv не найдено." -ForegroundColor Red
    Read-Host "Нажмите Enter для выхода"
    exit 1
}
if (-not (Test-Path $cloudflaredPath)) {
    Write-Host "ОШИБКА: tools\cloudflared.exe не найден." -ForegroundColor Red
    Read-Host "Нажмите Enter для выхода"
    exit 1
}

# 2. Временный запуск cloudflared для получения URL
Write-Host "[1/4] Запуск cloudflared для генерации HTTPS URL..." -ForegroundColor Yellow
$job = Start-Job -ScriptBlock {
    param($cfPath)
    & $cfPath tunnel --url http://localhost:8000
} -ArgumentList $cloudflaredPath

$httpsUrl = ""
Write-Host "Ожидаем генерацию URL (до 15 сек)..." -ForegroundColor Gray

# Ждем, пока в выводе не появится ссылка
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    $output = Receive-Job -Job $job
    $urlMatch = $output | Select-String -Pattern "https://[a-zA-Z0-9\-]+\.trycloudflare\.com" | Select-Object -First 1
    if ($urlMatch) {
        $httpsUrl = $urlMatch.Matches.Value
        break
    }
}

# Останавливаем временную задачу
Stop-Job $job | Out-Null
Remove-Job $job | Out-Null

if (-not $httpsUrl) {
    Write-Host "Не удалось получить URL от cloudflared. Проверьте интернет." -ForegroundColor Red
    Read-Host "Нажмите Enter для выхода"
    exit 1
}

Write-Host "Успешно получен URL: $httpsUrl" -ForegroundColor Green

# 3. Автоматическое обновление файла .env
Write-Host "[2/4] Обновление файла .env..." -ForegroundColor Yellow
$envContent = Get-Content $envPath -Raw
# Заменяем строку WEB_BASE_URL=... на новую
$newEnvContent = $envContent -replace '(?m)^WEB_BASE_URL=.*$', "WEB_BASE_URL=$httpsUrl"

if ($newEnvContent -ne $envContent) {
    # Сохраняем с той же кодировкой, что и оригинал (обычно UTF8 без BOM или UTF8)
    [System.IO.File]::WriteAllText($envPath, $newEnvContent, [System.Text.Encoding]::UTF8)
    Write-Host "Файл .env успешно обновлен." -ForegroundColor Green
} else {
    Write-Host "WEB_BASE_URL уже был равен актуальному URL." -ForegroundColor Gray
}

# 4. Запуск основных сервисов в отдельных окнах
Write-Host "[3/4] Запуск веб-сервера (Uvicorn)..." -ForegroundColor Yellow
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$projectRoot'; & '$venvPython' -m uvicorn app.main:app --host 0.0.0.0 --port 8000" -WindowStyle Normal

Start-Sleep -Seconds 2

Write-Host "[4/4] Запуск Cloudflare туннеля и Бота..." -ForegroundColor Yellow
# Запускаем cloudflared в отдельном окне, чтобы он работал постоянно
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$projectRoot'; & '$cloudflaredPath' tunnel --url http://localhost:8000" -WindowStyle Normal

Start-Sleep -Seconds 2

# Запускаем бота (он прочитает обновленный .env)
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$projectRoot'; & '$venvPython' -m bot.main" -WindowStyle Normal

Write-Host "========================================" -ForegroundColor Green
Write-Host "ВСЁ ЗАПУЩЕНО!" -ForegroundColor Green
Write-Host "URL в .env установлен в: $httpsUrl" -ForegroundColor Cyan
Write-Host ""
Write-Host "ВАЖНО: Если URL изменился, не забудьте обновить его в @BotFather:" -ForegroundColor Yellow
Write-Host "  1. Напишите /setmenubutton" -ForegroundColor Yellow
Write-Host "  2. Выберите бота" -ForegroundColor Yellow
Write-Host "  3. Вставьте эту ссылку: $httpsUrl" -ForegroundColor Yellow
Write-Host "========================================"
Write-Host "Это окно можно закрыть. Сервисы продолжат работать в своих окнах."
Read-Host "Нажмите Enter для выхода"