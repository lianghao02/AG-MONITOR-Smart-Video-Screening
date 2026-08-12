@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "YOLO_CONFIG_DIR=%~dp0captures\.ultralytics"
set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"
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

rem Defense 2: Use a supported system Python only to create the project .venv.
py -3 -c "import sys; sys.exit(sys.version_info[:2] not in [(3,10),(3,11),(3,12),(3,13)])" >nul 2>&1
if not errorlevel 1 goto CREATE_VENV_WITH_PY
python -c "import sys; sys.exit(sys.version_info[:2] not in [(3,10),(3,11),(3,12),(3,13)])" >nul 2>&1
if not errorlevel 1 goto CREATE_VENV_WITH_PYTHON
goto CHECK_EMBED

:CREATE_VENV_WITH_PY
echo [InBox] Creating isolated .venv with Python launcher...
py -3 -m venv ".venv"
if errorlevel 1 goto CHECK_EMBED
goto INSTALL_VENV

:CREATE_VENV_WITH_PYTHON
echo [InBox] Creating isolated .venv with system Python...
python -m venv ".venv"
if errorlevel 1 goto CHECK_EMBED

:INSTALL_VENV
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
