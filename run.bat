@echo off
REM ===================================================================
REM NexusDownloader Launcher
REM Consolidated launcher with dependency checking and auto-installation
REM ===================================================================

echo Starting NexusDownloader...
echo.

REM Change to script directory
cd /d "%~dp0"

REM ===================================================================
REM Python Installation Check and Setup
REM ===================================================================

REM Check if Python is available (prefer pythonw for GUI apps)
where pythonw.exe >nul 2>&1
if %ERRORLEVEL%==0 (
    set PYTHON_CMD=pythonw.exe
    goto :check_dependencies
)

REM Fallback to regular python
python --version >nul 2>&1
if %ERRORLEVEL%==0 (
    set PYTHON_CMD=python
    echo NOTE: Using python.exe - console window will remain visible
    echo For cleaner GUI experience, ensure pythonw.exe is available
    echo.
    goto :check_dependencies
)

REM Python not found - offer installation
echo ERROR: Python is not installed or not in PATH
echo.
echo NexusDownloader requires Python 3.8 or later to run.
echo.
choice /c YN /m "Would you like to download and install Python automatically"
if errorlevel 2 goto :manual_install
if errorlevel 1 goto :auto_install

:auto_install
echo.
echo Downloading Python installer...
powershell -Command "try { Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.3/python-3.12.3-amd64.exe' -OutFile 'python-installer.exe' } catch { exit 1 }"
if errorlevel 1 (
    echo ERROR: Failed to download Python installer
    echo Please check your internet connection and try again
    goto :manual_install
)

echo Installing Python (this may take a few minutes)...
echo Please wait for the installation to complete.
start /wait python-installer.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0 SimpleInstall=1

REM Clean up installer
if exist python-installer.exe del python-installer.exe

REM Verify installation
where pythonw.exe >nul 2>&1
if %ERRORLEVEL%==0 (
    set PYTHON_CMD=pythonw.exe
    echo Python installed successfully!
    echo.
    goto :check_dependencies
) else (
    echo ERROR: Python installation failed
    goto :manual_install
)

:manual_install
echo.
echo Please install Python manually:
echo 1. Visit: https://www.python.org/downloads/
echo 2. Download Python 3.8 or later
echo 3. During installation, make sure to check "Add Python to PATH"
echo 4. Run this script again after installation
echo.
pause
exit /b 1

REM ===================================================================
REM Dependency Installation
REM ===================================================================

:check_dependencies
echo Checking dependencies...

REM Upgrade pip
echo Updating pip...
%PYTHON_CMD% -m pip install --upgrade pip >nul 2>&1

REM Install from requirements.txt if it exists
if exist requirements.txt (
    echo Installing dependencies from requirements.txt...
    %PYTHON_CMD% -m pip install -r requirements.txt >nul 2>&1
    if errorlevel 1 (
        echo WARNING: Some dependencies from requirements.txt failed to install
    )
)

REM Check for PySide6 specifically
echo Checking for PySide6...
%PYTHON_CMD% -c "import PySide6" >nul 2>&1
if errorlevel 1 (
    echo Installing PySide6...
    %PYTHON_CMD% -m pip install PySide6>=6.4.0
    if errorlevel 1 (
        echo ERROR: Failed to install PySide6
        echo Please run manually: pip install PySide6>=6.4.0
        pause
        exit /b 1
    )
)

REM Check for requests library
%PYTHON_CMD% -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo Installing requests...
    %PYTHON_CMD% -m pip install requests
)

echo Dependencies check complete!
echo.

REM ===================================================================
REM Launch Application
REM ===================================================================

echo Launching NexusDownloader GUI...
echo.

REM Check if the GUI entry point exists
if not exist run_gui.py (
    echo ERROR: GUI entry point not found (run_gui.py)
    echo Please ensure you have the complete NexusDownloader package
    pause
    exit /b 1
)

REM Launch the application
if "%PYTHON_CMD%"=="pythonw.exe" (
    REM Silent launch with pythonw
    start "" %PYTHON_CMD% run_gui.py
    REM Brief pause to check for immediate startup errors
    timeout /t 2 /nobreak >nul
    echo NexusDownloader launched successfully!
    echo If the application doesn't appear, check for error messages above.
) else (
    REM Launch with python (console visible)
    %PYTHON_CMD% run_gui.py
    if errorlevel 1 (
        echo.
        echo Application closed with errors. Check the output above.
        pause
    )
)

exit /b 0