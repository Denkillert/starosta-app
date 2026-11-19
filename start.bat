@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run: python -m venv .venv
    pause
    exit /b 1
)

if not exist "tools\cloudflared.exe" (
    echo ERROR: tools\cloudflared.exe not found.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0auto-start.ps1"
