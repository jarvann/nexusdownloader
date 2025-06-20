@echo off
REM Check if pythonw is available
where pythonw.exe >nul 2>&1
if %ERRORLEVEL%==0 (
    REM Ensure required Python libraries are installed
    pythonw.exe -m pip install --upgrade pip >nul 2>&1
    pythonw.exe -m pip install -r requirements.txt >nul 2>&1

    start "" pythonw.exe .\src\gui.py
    exit /b
)

REM Pythonw.exe not found, prompt user to install Python
mshta "javascript:var sh=new ActiveXObject('WScript.Shell');var r=sh.Popup('Python is not installed or not in PATH. Download and install Python now?',0,'Python Required',4+32);close();r;" > "%temp%\pymsg.txt"
set /p userchoice=<"%temp%\pymsg.txt"
del "%temp%\pymsg.txt"

if "%userchoice%"=="6" (
    REM User clicked Yes
    echo Downloading latest Python installer...
    powershell -Command "Invoke-WebRequest -Uri https://www.python.org/ftp/python/3.12.3/python-3.12.3-amd64.exe -OutFile python-installer.exe"
    echo Installing Python silently...
    start /wait python-installer.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0 SimpleInstall=1
    del python-installer.exe
    REM Try again
    where pythonw.exe >nul 2>&1
    if %ERRORLEVEL%==0 (
        REM Ensure required Python libraries are installed after Python install
        pythonw.exe -m pip install --upgrade pip >nul 2>&1
        pythonw.exe -m pip install -r requirements.txt >nul 2>&1
        start "" pythonw.exe .\src\gui.py
    ) else (
        mshta "javascript:alert('Python installation failed. Please install Python manually.');close();"
    )
) else (
    mshta "javascript:alert('Python is required to run this application. Exiting.');close();"
)
exit /b