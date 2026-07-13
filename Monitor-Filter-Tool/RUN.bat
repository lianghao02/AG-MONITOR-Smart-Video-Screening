@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo =======================================================
echo     AG-MONITOR Forensic Player - Dual-Mode Launcher
echo =======================================================
echo.

rem [ Defense Line 1: Check Local Python Environment ]
where python >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Local Python environment detected.
    
    python -c "import eel; import av; import ultralytics; import cv2" >nul 2>&1
    if errorlevel 1 (
        echo [InBox] Installing required forensic modules in background... Please wait...
        python -m pip uninstall -y opencv-python >nul 2>&1
        python -m pip install eel ultralytics opencv-contrib-python av lap lapx
        python -c "import eel; import av; import ultralytics; import cv2; import lap" >nul 2>&1
        if errorlevel 1 (
            echo [!] Failed to install modules.
            echo [-^>] Switching to portable mode...
            goto CHECK_EMBED
        )
    )
    
    echo -------------------------------------------------------
    echo [OK] Core Engine Ready! Launching AG-MONITOR...
    echo.
    python -B -u main.py
    pause
    exit /b
)

:CHECK_EMBED
rem [ Defense Line 2: Check Portable Environment ]
if exist ".\python-embed\python.exe" (
    echo [OK] Portable core detected, starting [Portable Mode]...
    echo [OK] Booting up tactical room...
    echo -------------------------------------------------------
    .\python-embed\python.exe -B -u main.py
    pause
    exit /b
)

echo [FATAL ERROR] Dual-boot failed!
echo 1. Local Python missing required modules and failed to install.
echo 2. ".\python-embed\" portable core directory not found.
echo.
pause
exit /b
