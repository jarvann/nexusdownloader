@echo off
REM NexusDownloader Windows Build Script
REM Builds executable and creates installer using NSIS

setlocal enabledelayedexpansion

echo ====================================
echo  NexusDownloader Windows Build
echo ====================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.8+ from https://python.org
    exit /b 1
)

REM Check if we're in a virtual environment or create one
if "%VIRTUAL_ENV%"=="" (
    echo Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
) else (
    echo Using existing virtual environment: %VIRTUAL_ENV%
)

REM Upgrade pip and install build dependencies
echo Installing build dependencies...
python -m pip install --upgrade pip
pip install pyinstaller[encryption]
pip install -r requirements.txt

REM Clean previous builds
echo Cleaning previous builds...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"
if exist "installer\windows\*.exe" del /q "installer\windows\*.exe"

REM Create assets directory if it doesn't exist
if not exist "assets" mkdir assets

REM Create default icon if it doesn't exist
if not exist "assets\icon.ico" (
    echo Creating default icon...
    REM You can add icon creation logic here or provide instructions
    echo WARNING: No icon.ico found in assets directory
    echo Please add an icon file for better presentation
)

REM Build executable with PyInstaller
echo Building executable...
pyinstaller nexusdownloader.spec

if %errorlevel% neq 0 (
    echo ERROR: PyInstaller build failed
    exit /b 1
)

echo Build completed successfully!
echo Executable created in: dist\NexusDownloader\

REM Check if NSIS is available
where makensis >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo WARNING: NSIS not found in PATH
    echo To create installer:
    echo 1. Install NSIS from https://nsis.sourceforge.io/
    echo 2. Add NSIS to your PATH
    echo 3. Run: makensis installer\windows\nexusdownloader_installer.nsi
    echo.
    goto :skip_installer
)

REM Create installer
echo.
echo Creating Windows installer...
cd installer\windows
makensis nexusdownloader_installer.nsi

if %errorlevel% neq 0 (
    echo ERROR: Installer creation failed
    cd ..\..
    exit /b 1
)

cd ..\..
echo.
echo ====================================
echo Build completed successfully!
echo ====================================
echo.
echo Files created:
echo - Executable: dist\NexusDownloader\NexusDownloader.exe
echo - Installer: installer\windows\NexusDownloader_Setup_v2.0.0.exe
echo.

goto :end

:skip_installer
echo.
echo ====================================
echo Build completed!
echo ====================================
echo.
echo Files created:
echo - Executable: dist\NexusDownloader\NexusDownloader.exe
echo.
echo To create installer, install NSIS and run:
echo makensis installer\windows\nexusdownloader_installer.nsi
echo.

:end
pause