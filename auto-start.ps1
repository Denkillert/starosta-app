#requires -Version 5.1

# Запуск проекта "Староста":
#   1. веб-сервер (0.0.0.0:8000 — доступен и из локальной сети)
#   2. HTTPS-туннель: cloudflared, а если он не смог — SSH-туннель localhost.run
#      (мобильные операторы часто подменяют DNS для *.trycloudflare.com)
#   3. .env с актуальным адресом туннеля
#   4. бот (сам обновит кнопку меню в Telegram)
#
# TUNNEL_PROVIDER=auto|cloudflare|ssh — принудительный выбор туннеля.

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Полный лог запуска — пригодится, если что-то пойдёт не так.
$transcriptPath = Join-Path $env:TEMP 'starosta-start.log'
try {
    Start-Transcript -Path $transcriptPath -Append -Force | Out-Null
} catch {
    # Транскрипт вспомогательный: без него запуск тоже работает.
}

# Без этого любая ошибка закрывала бы окно вместе с текстом ошибки.
trap {
    Write-Host ""
    Write-Host "ОШИБКА: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.InvocationInfo) {
        Write-Host "Строка $($_.InvocationInfo.ScriptLineNumber): $($_.InvocationInfo.Line.Trim())" -ForegroundColor DarkGray
    }
    Write-Host "Лог: $transcriptPath" -ForegroundColor DarkGray
    Read-Host "Enter для выхода"
    exit 1
}

$root = $PSScriptRoot
$envPath = Join-Path $root '.env'
$cloudflaredPath = Join-Path $root 'tools\cloudflared.exe'
$pythonExe = Join-Path $root '.venv\Scripts\python.exe'
$port = 8000
$errLog = Join-Path $env:TEMP 'cloudflared.log'
$sshExe = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$sshLog = Join-Path $env:TEMP 'ssh-tunnel.log'
$sshErrLog = Join-Path $env:TEMP 'ssh-tunnel.err.log'
$knownHosts = Join-Path $env:TEMP 'starosta-known_hosts'
$watchScript = Join-Path $root 'tools\tunnel-watch.ps1'
$watchLog = Join-Path $env:TEMP 'starosta-watch.log'

$urlPatternCloudflare = 'https://[a-z0-9-]+\.trycloudflare\.com'
$urlPatternSsh = 'https://[a-z0-9-]+\.(?:app\.)?lhr\.life|https://[a-z0-9-]+\.(?:run\.)?pinggy(?:-free)?\.(?:link|net)'

function Wait-TunnelUrl {
    # Адрес туннеля появляется в логе не сразу, а файл в этот момент ещё
    # дописывается — поэтому исключение чтения не считается ошибкой.
    param(
        [string[]]$Paths,
        [string]$Pattern,
        [int]$Attempts
    )
    for ($i = 0; $i -lt $Attempts; $i++) {
        Start-Sleep -Milliseconds 500
        foreach ($path in $Paths) {
            if (-not (Test-Path -LiteralPath $path)) { continue }
            try {
                $text = Get-Content -LiteralPath $path -Raw -Encoding UTF8 -ErrorAction Stop
            } catch {
                continue
            }
            # Пустой файл Get-Content -Raw возвращает как $null, а не ''.
            if ([string]::IsNullOrEmpty($text)) { continue }
            $match = [regex]::Match($text, $Pattern)
            if ($match.Success) { return $match.Value }
        }
    }
    return $null
}

function Stop-Cloudflared {
    Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -eq $cloudflaredPath } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Write-Step([string]$text) {
    Write-Host ""
    Write-Host "== $text" -ForegroundColor Cyan
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Староста: запуск" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# --- Проверки -------------------------------------------------------------
if (-not (Test-Path $pythonExe)) {
    Write-Host "ОШИБКА: не найдено окружение .venv." -ForegroundColor Red
    Write-Host "Создайте его: python -m venv .venv"
    Write-Host "И установите зависимости: .venv\Scripts\pip install -r requirements.txt"
    Read-Host "Enter для выхода"
    exit 1
}
if (-not (Test-Path $cloudflaredPath)) {
    Write-Host "ОШИБКА: не найден tools\cloudflared.exe." -ForegroundColor Red
    Write-Host "Скачайте cloudflared-windows-amd64.exe и положите как tools\cloudflared.exe"
    Read-Host "Enter для выхода"
    exit 1
}

# --- Останавливаем предыдущий запуск -------------------------------------
Write-Step "Останавливаю предыдущий запуск (если был)"

Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -eq $cloudflaredPath } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# SSH-туннель localhost.run, запущенный прошлым стартом (чужие ssh-сессии не трогаем).
Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine -like '*localhost.run*' -or $_.CommandLine -like '*pinggy*') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# Сторож прошлого запуска: новый запуск поднимет его заново.
Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*tunnel-watch.ps1*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*-m bot.main*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    $proc = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -like 'python*') {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Milliseconds 500

# --- 1. Веб-сервер --------------------------------------------------------
Write-Step "[1/4] Веб-сервер на 0.0.0.0:$port"

$serverCmd = "`"$pythonExe`" -m uvicorn app.main:app --host 0.0.0.0 --port $port"
Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', $serverCmd -WorkingDirectory $root

$serverReady = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) { $serverReady = $true; break }
    } catch { }
}
if (-not $serverReady) {
    Write-Host "ПРЕДУПРЕЖДЕНИЕ: сервер не ответил на /health за 20 секунд." -ForegroundColor Yellow
    Write-Host "Смотрите окно 'Starosta Web' — там текст ошибки." -ForegroundColor Yellow
}

# --- 2. Туннель -----------------------------------------------------------
Write-Step "[2/4] Туннель"

$provider = $env:TUNNEL_PROVIDER
if (-not $provider) { $provider = 'auto' }
$tunnelUrl = $null
$tunnelKind = ''

if ($provider -ne 'ssh') {
    Remove-Item $errLog -ErrorAction SilentlyContinue
    $tunnelCmd = "`"$cloudflaredPath`" tunnel --url http://127.0.0.1:$port --logfile `"$errLog`" --loglevel info"
    # Дополнительные кавычки нужны cmd.exe, иначе он срезает первую и последнюю
    # кавычку и команда с двумя путями в кавычках ломается.
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', "`"$tunnelCmd`"" -WorkingDirectory $root
    $tunnelUrl = Wait-TunnelUrl -Paths @($errLog) -Pattern $urlPatternCloudflare -Attempts 40
    if ($tunnelUrl) { $tunnelKind = 'cloudflare' }
}

if (-not $tunnelUrl -and $provider -eq 'cloudflare') {
    Write-Host "ОШИБКА: cloudflared не выдал адрес, а резервный туннель отключён (TUNNEL_PROVIDER=cloudflare)." -ForegroundColor Red
    if (Test-Path $errLog) { Get-Content $errLog -Tail 15 -Encoding UTF8 }
    Read-Host "Enter для выхода"
    exit 1
}

if (-not $tunnelUrl) {
    Write-Host "  cloudflared не получил адрес." -ForegroundColor Yellow
    if (Test-Path $errLog) {
        $tail = Get-Content -LiteralPath $errLog -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
        if ($tail -match 'certificate|failed to request quick Tunnel') {
            Write-Host "  Причина: DNS/прокси провайдера. Мобильные операторы подменяют" -ForegroundColor Yellow
            Write-Host "  *.trycloudflare.com на свой сервер, и TLS-сертификат не сходится." -ForegroundColor Yellow
        }
    }
    Stop-Cloudflared

    if (-not (Test-Path $sshExe)) {
        Write-Host "ОШИБКА: не найден встроенный SSH-клиент Windows ($sshExe)." -ForegroundColor Red
        Write-Host "Включите компонент Windows «Клиент OpenSSH» либо запустите туннель" -ForegroundColor Red
        Write-Host "через другую сеть." -ForegroundColor Red
        Read-Host "Enter для выхода"
        exit 1
    }

    Write-Host "  Переключаюсь на SSH-туннель localhost.run (без регистрации)." -ForegroundColor Cyan
    Remove-Item $sshLog, $sshErrLog -ErrorAction SilentlyContinue
    # -T: без псевдотерминала, иначе localhost.run рисует QR-код и адрес
    # теряется в ANSI-последовательностях.
    $sshArguments = @(
        '-T',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', "UserKnownHostsFile=$knownHosts",
        '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=4',
        '-o', 'TCPKeepAlive=yes',
        '-o', 'ConnectTimeout=15',
        '-o', 'ExitOnForwardFailure=yes',
        '-R', "80:127.0.0.1:$port",
        'nokey@localhost.run'
    )
    Start-Process -FilePath $sshExe -ArgumentList $sshArguments `
        -RedirectStandardOutput $sshLog -RedirectStandardError $sshErrLog `
        -WindowStyle Hidden -WorkingDirectory $root
    $tunnelUrl = Wait-TunnelUrl -Paths @($sshLog, $sshErrLog) -Pattern $urlPatternSsh -Attempts 60
    if ($tunnelUrl) { $tunnelKind = 'ssh' }
}

if (-not $tunnelUrl) {
    Write-Host "ОШИБКА: туннель не выдал адрес." -ForegroundColor Red
    foreach ($path in @($errLog, $sshLog, $sshErrLog)) {
        if (Test-Path -LiteralPath $path) {
            Write-Host "Последние строки $path" -ForegroundColor Yellow
            Get-Content -LiteralPath $path -Tail 10 -Encoding UTF8
        }
    }
    Read-Host "Enter для выхода"
    exit 1
}

Write-Host "  $tunnelUrl" -ForegroundColor Green
if ($tunnelKind -eq 'ssh') {
    Write-Host "  (SSH-туннель localhost.run; работает, пока запущен ssh.exe)" -ForegroundColor DarkGray
} else {
    Write-Host "  (при каждом запуске адрес новый)" -ForegroundColor DarkGray
}

# --- 3. Обновляем .env ----------------------------------------------------
Write-Step "[3/4] Обновляю WEB_BASE_URL в .env"

$envText = [System.IO.File]::ReadAllText($envPath, [System.Text.Encoding]::UTF8)
if ($envText -match '(?m)^\s*WEB_BASE_URL\s*=') {
    $newEnvText = [regex]::Replace($envText, '(?m)^\s*WEB_BASE_URL\s*=.*$', "WEB_BASE_URL=$tunnelUrl")
} else {
    $newEnvText = $envText.TrimEnd() + "`r`nWEB_BASE_URL=$tunnelUrl`r`n"
}
# UTF-8 без BOM: pydantic-settings читает такой файл без сюрпризов.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($envPath, $newEnvText, $utf8NoBom)
Write-Host "  .env обновлён." -ForegroundColor Green

# --- 4. Бот ---------------------------------------------------------------
Write-Step "[4/4] Бот"

$botCmd = "`"$pythonExe`" -m bot.main"
Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', $botCmd -WorkingDirectory $root

# --- 5. Сторож -------------------------------------------------------------
# Следит, чтобы сервер, бот и туннель не оставались лежать, и переносит
# новый адрес localhost.run в .env (бесплатный тариф меняет домен сам).
if (Test-Path $watchScript) {
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', "`"$watchScript`"") `
        -WorkingDirectory $root -WindowStyle Hidden
} else {
    Write-Host "ПРЕДУПРЕЖДЕНИЕ: не найден tools\tunnel-watch.ps1 — автоперезапуск отключён." -ForegroundColor Yellow
}

# --- Итог -----------------------------------------------------------------
$lanIp = (& $pythonExe -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); print(s.getsockname()[0])") 2>$null
if (-not $lanIp) { $lanIp = '127.0.0.1' }

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  Запущено" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "  Mini App / интернет: $tunnelUrl"
Write-Host "  С телефона в Wi-Fi:  http://${lanIp}:$port"
Write-Host "  С этого ПК:          http://127.0.0.1:$port"
Write-Host ""
Write-Host "  Открыты окна: сервер и бот." -ForegroundColor DarkGray
if ($tunnelKind -eq 'ssh') {
    Write-Host "  Туннель работает в фоне (ssh.exe); следующий запуск его перезапустит." -ForegroundColor DarkGray
    Write-Host "  Лог туннеля: $sshLog" -ForegroundColor DarkGray
} else {
    Write-Host "  Лог туннеля: $errLog" -ForegroundColor DarkGray
}
Write-Host "  Сторож (перезапуск сервера/бота/туннеля): $watchLog" -ForegroundColor DarkGray
Write-Host "  Полностью остановить: stop.bat" -ForegroundColor DarkGray
Write-Host ""
Read-Host "Enter для закрытия этого окна"
