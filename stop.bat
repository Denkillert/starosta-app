@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Stopping Starosta: server, bot, tunnel and watcher...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*tunnel-watch.ps1*') -or ($_.Name -eq 'ssh.exe' -and ($_.CommandLine -like '*localhost.run*' -or $_.CommandLine -like '*pinggy*')) -or ($_.Name -eq 'cloudflared.exe' -and $_.ExecutablePath -like '*\tools\cloudflared.exe') -or ($_.Name -eq 'python.exe' -and ($_.CommandLine -like '*-m bot.main*' -or $_.CommandLine -like '*-m uvicorn app.main:app*')) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Done.
pause
