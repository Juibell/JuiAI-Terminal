@echo off
setlocal
set "PSModulePath="
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-JuiAI.ps1"
if errorlevel 1 (
    pause
    exit /b 1
)
