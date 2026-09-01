@echo off
title AG-MONITOR Smart Video Screening
cd /d "%~dp0."
set "PS_HOST=pwsh.exe"
where.exe pwsh.exe >nul 2>&1
if errorlevel 1 set "PS_HOST=powershell.exe"
"%PS_HOST%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_and_run.ps1"
if %errorlevel% neq 0 (
    echo.
    echo [Error] Startup failed. Please check error messages above or startup_error.log.
    pause
)
