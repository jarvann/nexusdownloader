; NexusDownloader Windows Installer Script
; Compatible with NSIS 3.0+

!define APP_NAME "NexusDownloader"
!define APP_VERSION "2.0.0"
!define APP_PUBLISHER "NexusDownloader Team"
!define APP_URL "https://github.com/nexusdownloader"
!define APP_EXECUTABLE "NexusDownloader.exe"
!define APP_DESCRIPTION "Advanced Nexus Mods collection downloader with GUI"

; Modern UI
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "WinVer.nsh"

; Application settings
Name "${APP_NAME}"
OutFile "NexusDownloader_Setup_v${APP_VERSION}.exe"
InstallDir "$PROGRAMFILES64\${APP_NAME}"
InstallDirRegKey HKLM "Software\${APP_NAME}" "InstallDir"
RequestExecutionLevel admin

; Version information
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey "FileDescription" "${APP_DESCRIPTION}"

; Modern UI Settings
!define MUI_ABORTWARNING
!define MUI_ICON "..\..\assets\icon.ico"
!define MUI_UNICON "..\..\assets\icon.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "..\..\assets\installer_banner.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "..\..\assets\installer_banner.bmp"

; Pages
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\..\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

; Languages
!insertmacro MUI_LANGUAGE "English"

; Functions
Function .onInit
    ; Check Windows version
    ${IfNot} ${AtLeastWin7}
        MessageBox MB_OK|MB_ICONSTOP "This application requires Windows 7 or later."
        Abort
    ${EndIf}
    
    ; Check for existing installation
    ReadRegStr $R0 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "UninstallString"
    StrCmp $R0 "" done
    
    MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION "${APP_NAME} is already installed. $\n$\nClick OK to remove the previous version or Cancel to cancel this upgrade." IDOK uninst
    Abort
    
    uninst:
        ClearErrors
        ExecWait '$R0 _?=$INSTDIR'
        IfErrors no_remove_label done
        no_remove_label:
            MessageBox MB_OK|MB_ICONSTOP "Failed to uninstall previous version."
            Abort
    done:
FunctionEnd

; Python dependency checker
Function CheckPython
    ; Check if Python is installed and accessible
    nsExec::ExecToStack 'python --version'
    Pop $0 ; return code
    Pop $1 ; output
    
    ${If} $0 != 0
        MessageBox MB_YESNO|MB_ICONQUESTION "Python is not installed or not in PATH. $\n$\nWould you like to download and install Python automatically? $\n$\n(This requires an internet connection)" IDYES install_python IDNO skip_python
        
        install_python:
            DetailPrint "Downloading Python installer..."
            inetc::get "https://www.python.org/ftp/python/3.11.0/python-3.11.0-amd64.exe" "$TEMP\python_installer.exe" /end
            Pop $R0
            ${If} $R0 == "OK"
                DetailPrint "Installing Python..."
                ExecWait '"$TEMP\python_installer.exe" /quiet InstallAllUsers=1 PrependPath=1 Include_test=0'
                Delete "$TEMP\python_installer.exe"
                DetailPrint "Python installation completed."
            ${Else}
                MessageBox MB_OK|MB_ICONEXCLAMATION "Failed to download Python. Please install Python manually from https://python.org"
            ${EndIf}
            Goto end_python_check
            
        skip_python:
            MessageBox MB_OK|MB_ICONWARNING "Python is required for ${APP_NAME} to work properly. Please install Python 3.8 or later."
    ${Else}
        DetailPrint "Python is already installed: $1"
    ${EndIf}
    
    end_python_check:
FunctionEnd

; Installation sections
Section "Core Application" SecCore
    SectionIn RO  ; Read only - always installed
    
    SetOutPath "$INSTDIR"
    
    ; Check Python dependency
    Call CheckPython
    
    ; Install main application files
    File /r "..\..\dist\NexusDownloader\*"
    
    ; Create application data directory
    CreateDirectory "$APPDATA\${APP_NAME}"
    CreateDirectory "$APPDATA\${APP_NAME}\logs"
    
    ; Write registry keys
    WriteRegStr HKLM "Software\${APP_NAME}" "InstallDir" "$INSTDIR"
    WriteRegStr HKLM "Software\${APP_NAME}" "Version" "${APP_VERSION}"
    
    ; Write uninstaller
    WriteUninstaller "$INSTDIR\Uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayName" "${APP_NAME}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "UninstallString" "$INSTDIR\Uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayIcon" "$INSTDIR\${APP_EXECUTABLE},0"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "Publisher" "${APP_PUBLISHER}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "URLInfoAbout" "${APP_URL}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayVersion" "${APP_VERSION}"
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "NoModify" 1
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "NoRepair" 1
    
    ; Calculate installed size
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "EstimatedSize" "$0"
SectionEnd

Section "Desktop Shortcut" SecDesktop
    CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXECUTABLE}" "" "$INSTDIR\${APP_EXECUTABLE}" 0
SectionEnd

Section "Start Menu Shortcut" SecStartMenu
    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXECUTABLE}" "" "$INSTDIR\${APP_EXECUTABLE}" 0
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Add to PATH" SecPath
    ; Add installation directory to system PATH
    ReadRegStr $R1 HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" "PATH"
    StrCpy $R2 "$R1;$INSTDIR"
    WriteRegExpandStr HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" "PATH" $R2
    
    ; Notify system of environment change
    SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=5000
SectionEnd

; Section descriptions
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${SecCore} "Core application files (required)"
    !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "Create a desktop shortcut"
    !insertmacro MUI_DESCRIPTION_TEXT ${SecStartMenu} "Create start menu shortcuts"
    !insertmacro MUI_DESCRIPTION_TEXT ${SecPath} "Add NexusDownloader to system PATH"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; Uninstaller
Section "Uninstall"
    ; Remove application files
    RMDir /r "$INSTDIR"
    
    ; Remove shortcuts
    Delete "$DESKTOP\${APP_NAME}.lnk"
    RMDir /r "$SMPROGRAMS\${APP_NAME}"
    
    ; Remove from PATH
    ReadRegStr $R1 HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" "PATH"
    ${StrRep} $R2 $R1 ";$INSTDIR" ""
    ${StrRep} $R3 $R2 "$INSTDIR;" ""
    ${StrRep} $R4 $R3 "$INSTDIR" ""
    WriteRegExpandStr HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" "PATH" $R4
    
    ; Remove registry keys
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
    DeleteRegKey HKLM "Software\${APP_NAME}"
    
    ; Note: We don't remove user data directory to preserve user settings and logs
    MessageBox MB_YESNO "Remove user data and settings? $\n$\n(Logs and configuration files in $APPDATA\${APP_NAME})" IDNO skip_userdata
    RMDir /r "$APPDATA\${APP_NAME}"
    skip_userdata:
    
    ; Notify system of environment change
    SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=5000
SectionEnd