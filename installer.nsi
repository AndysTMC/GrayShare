!include "MUI2.nsh"

!define APP_NAME "GrayShare"
!define APP_EXE "GrayShare.exe"
!define APP_PUBLISHER "AndysTMC"
!define APP_VERSION "1.0.0"
!define APP_VERSION_DWORD "1.0.0.0"
!define APP_ICON "static\installer.ico"

!ifndef SOURCE_DIST_PATH
  !define SOURCE_DIST_PATH "dist"
!endif

!ifndef OUTPUT_DIST_PATH
  !define OUTPUT_DIST_PATH "dist"
!endif

Name "${APP_NAME}"
OutFile "${OUTPUT_DIST_PATH}\GrayShare-Setup.exe"
InstallDir "$PROGRAMFILES64\GrayShare"
InstallDirRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "InstallLocation"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
Icon "${APP_ICON}"
UninstallIcon "${APP_ICON}"
BrandingText "GrayShare Setup"
VIProductVersion "${APP_VERSION_DWORD}"
VIAddVersionKey /LANG=1033 "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=1033 "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey /LANG=1033 "FileDescription" "GrayShare Installer"
VIAddVersionKey /LANG=1033 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "InternalName" "GrayShareSetup"
VIAddVersionKey /LANG=1033 "OriginalFilename" "GrayShare-Setup.exe"
VIAddVersionKey /LANG=1033 "LegalCopyright" "AndysTMC"

!define MUI_ICON "${APP_ICON}"
!define MUI_UNICON "${APP_ICON}"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Launch GrayShare"

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install"
  SetRegView 64
  SetOutPath "$INSTDIR"
  File "${SOURCE_DIST_PATH}\${APP_EXE}"

  CreateDirectory "$SMPROGRAMS\GrayShare"
  CreateShortCut "$SMPROGRAMS\GrayShare\GrayShare.lnk" "$INSTDIR\${APP_EXE}"
  CreateShortCut "$DESKTOP\GrayShare.lnk" "$INSTDIR\${APP_EXE}"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetRegView 64
  Delete "$DESKTOP\GrayShare.lnk"
  Delete "$SMPROGRAMS\GrayShare\GrayShare.lnk"
  RMDir "$SMPROGRAMS\GrayShare"

  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  RMDir /r "$PROFILE\.grayshare"

  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"
SectionEnd
