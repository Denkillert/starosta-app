#requires -Version 5.1

<#
Сторож запуска: веб-сервер, бот и туннель.

Зачем он нужен:
  * localhost.run на бесплатном тарифе регулярно меняет домен
    («tunnel ... removed» и новый адрес в ssh-tunnel.log), при этом
    ssh.exe остаётся жив, а ссылка в .env и кнопке Telegram умирает;
  * окно с сервером или ботом можно случайно закрыть, и тогда туннель
    честно показывает 502 «no tunnel».

Раз в 20 секунд сторож проверяет:
  1. http://127.0.0.1:8000/health — жив ли веб-сервер;
  2. python -m bot.main — жив ли бот;
  3. ssh.exe / cloudflared — жив ли туннель и не сменился ли адрес.

Сменился адрес — переписывает WEB_BASE_URL в .env и обновляет кнопку меню
Telegram (python -m tools.update_menu). Работающие сервер и бот подхватывают
новый адрес сами (app/services/tunnel.py), перезапускать их не нужно.

Лог: %TEMP%\starosta-watch.log
Остановить: закрыть powershell.exe со строкой tunnel-watch.ps1
(следующий запуск start.bat делает это сам).
#>

param(
    [int]$Port = 8000,
    [int]$IntervalSeconds = 20,
    [string]$Root = ''
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
# Python-подпроцессы (update_menu) должны писать в лог UTF-8, иначе
# кириллица превращается в «?????».
$env:PYTHONIOENCODING = 'utf-8'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $Root) { $Root = Split-Path $PSScriptRoot -Parent }

$pythonExe = Join-Path $Root '.venv\Scripts\python.exe'
$cloudflaredPath = Join-Path $Root 'tools\cloudflared.exe'
$sshExe = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$knownHosts = Join-Path $env:TEMP 'starosta-known_hosts'
$envPath = Join-Path $Root '.env'
$sshLog = Join-Path $env:TEMP 'ssh-tunnel.log'
$sshErrLog = Join-Path $env:TEMP 'ssh-tunnel.err.log'
$cfLog = Join-Path $env:TEMP 'cloudflared.log'
$watchLog = Join-Path $env:TEMP 'starosta-watch.log'
$serverOut = Join-Path $env:TEMP 'starosta-server.out.log'
$serverErr = Join-Path $env:TEMP 'starosta-server.err.log'
$botOut = Join-Path $env:TEMP 'starosta-bot.out.log'
$botErr = Join-Path $env:TEMP 'starosta-bot.err.log'

$urlPatternSsh = 'https://[a-z0-9-]+\.(?:app\.)?lhr\.life|https://[a-z0-9-]+\.(?:run\.)?pinggy(?:-free)?\.(?:link|net)'
$urlPatternCloudflare = 'https://[a-z0-9-]+\.trycloudflare\.com'

# Один сторож на сеанс: повторный запуск не должен плодить процессы.
$mutex = New-Object System.Threading.Mutex($false, 'Local\StarostaTunnelWatch')
try {
    if (-not $mutex.WaitOne(0)) { exit 0 }
} catch [System.Threading.AbandonedMutexException] {
    # Прошлого сторожа убили (start.bat делает это при перезапуске) —
    # мьютекс освободился вместе с процессом, работаем дальше.
}

function Write-Watch([string]$text) {
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $text
    try { Add-Content -LiteralPath $watchLog -Value $line -Encoding UTF8 } catch { }
}

function Get-Processes([string]$Name, [string]$Like) {
    @(
        Get-CimInstance Win32_Process -Filter "Name = '$Name'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -like $Like }
    )
}

function Get-CloudflaredProcesses {
    @(
        Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -eq $cloudflaredPath }
    )
}

function Test-LocalServer {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 5
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Test-PublicUrl([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 15
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Get-LastUrl([string]$Path, [string]$Pattern) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $text = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 -ErrorAction Stop
    } catch {
        return $null
    }
    if ([string]::IsNullOrEmpty($text)) { return $null }
    $found = [regex]::Matches($text, $Pattern)
    if ($found.Count -eq 0) { return $null }
    return $found[$found.Count - 1].Value
}

function Get-EnvUrl {
    if (-not (Test-Path -LiteralPath $envPath)) { return $null }
    $line = Select-String -LiteralPath $envPath -Pattern '^\s*WEB_BASE_URL\s*=' -Encoding UTF8 |
        Select-Object -Last 1
    if (-not $line) { return $null }
    return ($line.Line -replace '^\s*WEB_BASE_URL\s*=\s*', '').Trim()
}

function Set-EnvUrl([string]$Url) {
    $text = [System.IO.File]::ReadAllText($envPath, [System.Text.Encoding]::UTF8)
    if ($text -match '(?m)^\s*WEB_BASE_URL\s*=') {
        $newText = [regex]::Replace($text, '(?m)^\s*WEB_BASE_URL\s*=.*$', "WEB_BASE_URL=$Url")
    } else {
        $newText = $text.TrimEnd() + "`r`nWEB_BASE_URL=$Url`r`n"
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($envPath, $newText, $utf8NoBom)
}

function Update-PublicUrl([string]$Url) {
    $current = Get-EnvUrl
    if ($current -eq $Url) { return }
    Set-EnvUrl $Url
    Write-Watch "Новый адрес туннеля: $Url (был: $current)"
    try {
        & $pythonExe -m tools.update_menu 2>&1 | ForEach-Object { Write-Watch "  $_" }
    } catch {
        Write-Watch "  кнопка меню не обновлена: $($_.Exception.Message)"
    }
}

function Start-Server([string]$Reason) {
    Write-Watch "Перезапускаю веб-сервер ($Reason)"
    Start-Process -FilePath $pythonExe `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', "$Port") `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput $serverOut -RedirectStandardError $serverErr
}

function Start-Bot([string]$Reason) {
    Write-Watch "Перезапускаю бота ($Reason)"
    Start-Process -FilePath $pythonExe `
        -ArgumentList @('-m', 'bot.main') `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput $botOut -RedirectStandardError $botErr
}

function Stop-SshTunnels {
    foreach ($proc in (Get-Processes 'ssh.exe' '*localhost.run*')) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    foreach ($proc in (Get-Processes 'ssh.exe' '*pinggy*')) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Start-SshTunnel {
    Write-Watch 'Перезапускаю SSH-туннель localhost.run'
    Stop-SshTunnels
    Start-Sleep -Milliseconds 700
    Remove-Item -LiteralPath $sshLog, $sshErrLog -ErrorAction SilentlyContinue
    $sshArguments = @(
        '-T',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', "UserKnownHostsFile=$knownHosts",
        '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=4',
        '-o', 'TCPKeepAlive=yes',
        '-o', 'ConnectTimeout=15',
        '-o', 'ExitOnForwardFailure=yes',
        '-R', "80:127.0.0.1:$Port",
        'nokey@localhost.run'
    )
    Start-Process -FilePath $sshExe -ArgumentList $sshArguments `
        -RedirectStandardOutput $sshLog -RedirectStandardError $sshErrLog `
        -WindowStyle Hidden -WorkingDirectory $Root
}

function Start-CloudflareTunnel {
    Write-Watch 'Перезапускаю cloudflared'
    foreach ($proc in (Get-CloudflaredProcesses)) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 700
    Remove-Item -LiteralPath $cfLog -ErrorAction SilentlyContinue
    $tunnelCmd = "`"$cloudflaredPath`" tunnel --url http://127.0.0.1:$Port --logfile `"$cfLog`" --loglevel info"
    # Кавычки вокруг всей команды нужны cmd.exe, иначе он срезает крайние
    # кавычки у пути к cloudflared.exe.
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$tunnelCmd`"" `
        -WorkingDirectory $Root -WindowStyle Hidden
}

# --- Старт -----------------------------------------------------------------
if (Test-Path -LiteralPath $watchLog) {
    try {
        if ((Get-Item -LiteralPath $watchLog).Length -gt 1048576) {
            Move-Item -LiteralPath $watchLog -Destination "$watchLog.1" -Force
        }
    } catch { }
}
Write-Watch "Сторож запущен (порт $Port, интервал $IntervalSeconds с)"

$serverMisses = 0
$tunnelMisses = 0
$lastServerStart = [datetime]::MinValue
$lastBotStart = [datetime]::MinValue
$lastTunnelStart = [datetime]::MinValue

while ($true) {
    try {
        # 1. Веб-сервер -----------------------------------------------------
        $serverOk = Test-LocalServer
        if ($serverOk) {
            $serverMisses = 0
        } else {
            $serverMisses++
            if ($serverMisses -ge 3 -and ((Get-Date) - $lastServerStart).TotalSeconds -ge 60) {
                Start-Server 'нет ответа на /health'
                $lastServerStart = Get-Date
                $serverMisses = 0
                Start-Sleep -Seconds 5
                continue
            }
        }

        # 2. Бот ------------------------------------------------------------
        $botAlive = (Get-Processes 'python.exe' '*-m bot.main*').Count -gt 0
        if (-not $botAlive -and ((Get-Date) - $lastBotStart).TotalSeconds -ge 60) {
            Start-Bot 'процесс не найден'
            $lastBotStart = Get-Date
        }

        # 3. Туннель --------------------------------------------------------
        if (-not (Test-LocalServer)) { Start-Sleep -Seconds $IntervalSeconds; continue }

        $sshProcesses = @(Get-Processes 'ssh.exe' '*localhost.run*')
        $cfProcesses = @(Get-CloudflaredProcesses)

        if ($sshProcesses.Count -gt 0) {
            $url = Get-LastUrl $sshLog $urlPatternSsh
            if (-not $url) {
                # Туннель только что поднялся и ещё не напечатал адрес.
                if (((Get-Date) - $lastTunnelStart).TotalSeconds -ge 90) {
                    Start-SshTunnel
                    $lastTunnelStart = Get-Date
                }
            } elseif (Test-PublicUrl $url) {
                $tunnelMisses = 0
                Update-PublicUrl $url
            } else {
                $tunnelMisses++
                Write-Watch "Адрес $url не отвечает ($tunnelMisses/3)"
                if ($tunnelMisses -ge 3 -and ((Get-Date) - $lastTunnelStart).TotalSeconds -ge 60) {
                    Start-SshTunnel
                    $lastTunnelStart = Get-Date
                    $tunnelMisses = 0
                }
            }
        } elseif ($cfProcesses.Count -gt 0) {
            $url = Get-LastUrl $cfLog $urlPatternCloudflare
            if ($url) {
                # cloudflared проверяем по логу: у части провайдеров
                # *.trycloudflare.com подменяется DNS и localhost-проверка
                # врёт. Процесс жив и адрес новый — этого достаточно.
                Update-PublicUrl $url
            } elseif (((Get-Date) - $lastTunnelStart).TotalSeconds -ge 90) {
                Start-CloudflareTunnel
                $lastTunnelStart = Get-Date
            }
        } else {
            # Туннель умер. Поднимаем тот же вид, что был в прошлый раз.
            $previous = Get-EnvUrl
            if ($previous -like '*trycloudflare*') {
                Start-CloudflareTunnel
            } else {
                Start-SshTunnel
            }
            $lastTunnelStart = Get-Date
            Start-Sleep -Seconds 5
        }
    } catch {
        Write-Watch "Ошибка сторожа: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds $IntervalSeconds
}
