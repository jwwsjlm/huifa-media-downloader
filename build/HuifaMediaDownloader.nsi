; NSIS assisted wizard, matching Motrix's Windows installer UX.
; The embedded Velopack MSI owns files, shortcuts and the uninstall registration.
Unicode true
ManifestDPIAware true
RequestExecutionLevel admin

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

!ifndef AppVersion
  !error "AppVersion is required"
!endif
!ifndef AppFileVersion
  !error "AppFileVersion (four numeric parts) is required"
!endif
!ifndef SourceMsi
  !error "SourceMsi is required"
!endif
!ifndef OutputDir
  !error "OutputDir is required"
!endif
!ifndef AppIcon
  !error "AppIcon is required"
!endif

Name "HuifaMediaDownloader"
OutFile "${OutputDir}\HuifaMediaDownloader-Setup.exe"
InstallDir "$PROGRAMFILES64\HuifaMediaDownloader"
ShowInstDetails show
!define MUI_ICON "${AppIcon}"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\HuifaMediaDownloader.exe"
!define MUI_FINISHPAGE_REBOOTLATER_DEFAULT

; No pre-page callback: the directory page is always shown interactively.
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

VIProductVersion "${AppFileVersion}"
VIAddVersionKey /LANG=0 "ProductName" "HuifaMediaDownloader"
VIAddVersionKey /LANG=0 "ProductVersion" "${AppVersion}"
VIAddVersionKey /LANG=0 "FileVersion" "${AppVersion}"
VIAddVersionKey /LANG=0 "CompanyName" "Huifa"
VIAddVersionKey /LANG=0 "FileDescription" "HuifaMediaDownloader Setup"
VIAddVersionKey /LANG=0 "LegalCopyright" "Copyright © 2026 Huifa"

Var MsiExitCode
LangString UnsupportedSystem ${LANG_SIMPCHINESE} "需要支持 x64 应用的 Windows 系统。"
LangString UnsupportedSystem ${LANG_ENGLISH} "Windows with x64 application support is required."
LangString ExtractFailed ${LANG_SIMPCHINESE} "无法解压安装文件，请检查临时目录权限和剩余空间。"
LangString ExtractFailed ${LANG_ENGLISH} "Cannot extract the installer. Check temporary-folder permissions and free space."
LangString LaunchFailed ${LANG_SIMPCHINESE} "无法启动 Windows Installer，安装未完成。"
LangString LaunchFailed ${LANG_ENGLISH} "Cannot start Windows Installer. Installation did not complete."
LangString InstallFailed ${LANG_SIMPCHINESE} "安装未完成，Windows Installer 错误码：$MsiExitCode。$\r$\n日志：$TEMP\HuifaMediaDownloader-install.log"
LangString InstallFailed ${LANG_ENGLISH} "Installation did not complete. Windows Installer error: $MsiExitCode.$\r$\nLog: $TEMP\HuifaMediaDownloader-install.log"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "$(UnsupportedSystem)" /SD IDOK
    SetErrorLevel 1633
    Quit
  ${EndIf}
FunctionEnd

Section "HuifaMediaDownloader"
  ClearErrors
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=HuifaMediaDownloader.msi "${SourceMsi}"
  ${If} ${Errors}
    MessageBox MB_OK|MB_ICONSTOP "$(ExtractFailed)" /SD IDOK
    SetErrorLevel 1603
    Quit
  ${EndIf}

  ClearErrors
  ExecWait '"$SYSDIR\msiexec.exe" /i "$PLUGINSDIR\HuifaMediaDownloader.msi" VELOPACK_INSTALLDIR="$INSTDIR" ALLUSERS=1 MSIINSTALLPERUSER="" /qn /norestart /L*v "$TEMP\HuifaMediaDownloader-install.log"' $MsiExitCode
  ${If} ${Errors}
    MessageBox MB_OK|MB_ICONSTOP "$(LaunchFailed)" /SD IDOK
    SetErrorLevel 1603
    Quit
  ${EndIf}
  ${If} $MsiExitCode == 3010
    SetRebootFlag true
  ${ElseIf} $MsiExitCode != 0
    MessageBox MB_OK|MB_ICONSTOP "$(InstallFailed)" /SD IDOK
    SetErrorLevel $MsiExitCode
    Quit
  ${EndIf}
  SetErrorLevel $MsiExitCode
SectionEnd
