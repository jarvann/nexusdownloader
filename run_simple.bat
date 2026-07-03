@echo off
REM ===================================================================
REM NexusDownloader Simple Launcher
REM Minimal launcher for users with Python already installed
REM ===================================================================

echo Starting NexusDownloader...
cd /d "%~dp0"

REM Quick dependency check
python -c "import PySide6" >nul 2>&1
if errorlevel 1 (
    echo Installing PySide6...
    pip install PySide6>=6.4.0
)

REM Launch application
echo Launching GUI...
python run_gui.py

if errorlevel 1 (
    echo.
    echo Application closed with errors.
    pause
)