@echo off
title AG-MONITOR Smart Video Screening
cd /d "%~dp0."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_and_run.ps1"
if %errorlevel% neq 0 (
    echo.
    echo [Error] Startup failed. Please check error messages above or startup_error.log.
    pause
)
