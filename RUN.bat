@echo off
REM ==============================================================================
REM  專案名稱：AG-MONITOR 科技偵查工作站
REM  主要功能：一鍵智慧自癒啟動（免裝 Python、自動下載配置、隨身碟即插即用）
REM ==============================================================================

title 正在啟動 AG-MONITOR 科技偵查工作站...
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_and_run.ps1"
if %errorlevel% neq 0 (
    echo.
    echo [錯誤] 啟動失敗，請檢視上方錯誤訊息。
    pause
)
