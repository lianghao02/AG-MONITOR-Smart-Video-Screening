@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo =======================================================
echo     AG-MONITOR Forensic Player - Dual-Mode Launcher
echo =======================================================
echo.

set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"
set "PYTHON_EXE="

rem [ Defense Line 1: Check existing .venv Virtual Environment ]
if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=%VENV_PYTHON%"
        echo [OK] Found project .venv environment.
        goto RUN_ENGINE
    )
    echo [!] Existing .venv is invalid; attempting repair.
)

rem [ Defense Line 2: Discover System Python (Filtering out WindowsApps alias) ]
for /f "delims=" %%I in ('py -3 -c "import sys; p=sys.executable; print(p) if 'windowsapps' not in p.lower() else None" 2^>nul') do (
    if exist "%%I" set "PYTHON_EXE=%%I"
)

if not defined PYTHON_EXE (
    if exist "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
    if exist "%LOCALAPPDATA%\Python\pythoncore-3.12-64\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Python\pythoncore-3.12-64\python.exe"
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)

if defined PYTHON_EXE (
    echo [OK] Detected valid system Python: %PYTHON_EXE%
    echo [InBox] Creating isolated .venv environment for AG-MONITOR...
    "%PYTHON_EXE%" -m venv "%~dp0.venv"
    if not errorlevel 1 (
        "%VENV_PYTHON%" -c "import sys" >nul 2>&1
    )
    if not errorlevel 1 (
        set "PYTHON_EXE=%VENV_PYTHON%"
        echo [OK] Virtual environment created successfully.
    ) else (
        echo [!] Failed to create a usable .venv; switching to portable mode.
        goto CHECK_EMBED
    )
    goto RUN_ENGINE
)

:CHECK_EMBED
rem [ Defense Line 3: Check Portable Environment ]
if exist ".\python-embed\python.exe" (
    set "PYTHON_EXE=.\python-embed\python.exe"
    echo [OK] Portable core detected, starting [Portable Mode]...
    goto RUN_ENGINE
)

echo [FATAL ERROR] Dual-boot failed!
echo 1. Local Python missing required modules and failed to install.
echo 2. ".\python-embed\" portable core directory not found.
echo.
pause
exit /b

:RUN_ENGINE
"%PYTHON_EXE%" -c "import eel; import av; import ultralytics; import cv2; import lap" >nul 2>&1
if errorlevel 1 (
    echo [InBox] Installing required forensic modules in background... Please wait...
    "%PYTHON_EXE%" -m pip uninstall -y opencv-python >nul 2>&1
    "%PYTHON_EXE%" -m pip install eel ultralytics opencv-contrib-python av lap lapx
    if errorlevel 1 echo [!] pip failed to install required modules.
    "%PYTHON_EXE%" -c "import eel; import av; import ultralytics; import cv2; import lap" >nul 2>&1
    if errorlevel 1 (
        echo [!] Required modules are still unavailable.
        if not "%PYTHON_EXE%"==".\python-embed\python.exe" goto CHECK_EMBED
        echo [FATAL ERROR] Portable core is incomplete and dependency installation failed.
        pause
        exit /b 1
    )
)

echo -------------------------------------------------------
echo [OK] Core Engine Ready! Launching AG-MONITOR...
echo.
"%PYTHON_EXE%" -B -u main.py
set "APP_EXIT=%ERRORLEVEL%"
pause
exit /b %APP_EXIT%
