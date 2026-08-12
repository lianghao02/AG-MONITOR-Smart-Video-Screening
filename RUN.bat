@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "YOLO_CONFIG_DIR=%~dp0captures\.ultralytics"
set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"
set "SYSTEM_PYTHON="
if /I "%~1"=="--verify-only" set "VERIFY_ONLY=1"
if not exist "%YOLO_CONFIG_DIR%" mkdir "%YOLO_CONFIG_DIR%" >nul 2>&1

echo =======================================================
echo     AG-MONITOR Forensic Player - Dual-Mode Launcher
echo =======================================================
echo.

rem Defense 1: Prefer the isolated project environment and verify pinned versions.
if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" verify_runtime.py >nul 2>&1
    if not errorlevel 1 goto RUN_VENV
    echo [InBox] Repairing project virtual environment...
    "%VENV_PYTHON%" -m pip install --disable-pip-version-check --upgrade -r requirements.txt
    if not errorlevel 1 (
        "%VENV_PYTHON%" verify_runtime.py >nul 2>&1
        if not errorlevel 1 goto RUN_VENV
    )
    echo [!] Project virtual environment remains unavailable.
)

rem Defense 2: Discover a real Python executable and reject WindowsApps aliases.
for /f "delims=" %%I in ('py -3 -c "import sys; p=sys.executable; print(p) if 'windowsapps' not in p.lower() else None" 2^>nul') do if exist "%%I" set "SYSTEM_PYTHON=%%I"
if not defined SYSTEM_PYTHON if exist "%LOCALAPPDATA%\Python\pythoncore-3.13-64\python.exe" set "SYSTEM_PYTHON=%LOCALAPPDATA%\Python\pythoncore-3.13-64\python.exe"
if not defined SYSTEM_PYTHON if exist "%LOCALAPPDATA%\Python\pythoncore-3.12-64\python.exe" set "SYSTEM_PYTHON=%LOCALAPPDATA%\Python\pythoncore-3.12-64\python.exe"
if not defined SYSTEM_PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "SYSTEM_PYTHON=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined SYSTEM_PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "SYSTEM_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

if defined SYSTEM_PYTHON (
    "%SYSTEM_PYTHON%" -c "import sys; sys.exit(sys.version_info[:2] not in [(3,10),(3,11),(3,12),(3,13)])" >nul 2>&1
    if not errorlevel 1 goto CREATE_VENV
)
goto CHECK_EMBED

:CREATE_VENV
echo [InBox] Creating isolated .venv with %SYSTEM_PYTHON%...
"%SYSTEM_PYTHON%" -m venv ".venv"
if errorlevel 1 goto CHECK_EMBED
"%VENV_PYTHON%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto CHECK_EMBED
"%VENV_PYTHON%" verify_runtime.py >nul 2>&1
if errorlevel 1 goto CHECK_EMBED

:RUN_VENV
if defined VERIFY_ONLY (
    echo [OK] Project environment verification completed.
    exit /b 0
)
echo -------------------------------------------------------
echo [OK] Verified project environment. Launching AG-MONITOR...
echo.
"%VENV_PYTHON%" -B -u main.py
set "APP_EXIT=%ERRORLEVEL%"
pause
exit /b %APP_EXIT%

:CHECK_EMBED
rem Defense 3: The portable runtime must pass the same version and import checks.
if not exist ".\python-embed\python.exe" goto BOOT_FAILED
echo [OK] Portable core detected.
.\python-embed\python.exe verify_runtime.py >nul 2>&1
if not errorlevel 1 goto RUN_EMBED
echo [InBox] Portable core is incomplete. Repairing pinned modules...
.\python-embed\python.exe -m pip install --disable-pip-version-check --upgrade --force-reinstall --no-deps -r portable-requirements.txt
if errorlevel 1 goto BOOT_FAILED
.\python-embed\python.exe verify_runtime.py >nul 2>&1
if errorlevel 1 goto BOOT_FAILED

:RUN_EMBED
if defined VERIFY_ONLY (
    echo [OK] Portable environment verification completed.
    exit /b 0
)
echo -------------------------------------------------------
echo [OK] Verified portable environment. Launching AG-MONITOR...
echo.
.\python-embed\python.exe -B -u main.py
set "APP_EXIT=%ERRORLEVEL%"
pause
exit /b %APP_EXIT%

:BOOT_FAILED
echo [FATAL ERROR] No verified AG-MONITOR Python environment is available.
echo Please confirm network access or repair the python-embed directory.
echo.
pause
exit /b 1
