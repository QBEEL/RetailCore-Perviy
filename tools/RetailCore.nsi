Unicode True
RequestExecutionLevel user

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

!define UninstallKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\RetailCore"
Name "RetailCore"
OutFile "${RetailCoreSetup}"
InstallDir "$LOCALAPPDATA\Programs\RetailCore"
InstallDirRegKey HKCU "${UninstallKey}" "InstallLocation"
SetCompressor zlib

VIProductVersion "${RetailCoreVersion}.0"
VIAddVersionKey "ProductName" "RetailCore"
VIAddVersionKey "FileDescription" "RetailCore Setup"
VIAddVersionKey "FileVersion" "${RetailCoreVersion}"
VIAddVersionKey "LegalCopyright" "RetailCore"

!define MUI_ICON "..\app\ui\assets\app.ico"
!define MUI_UNICON "${MUI_ICON}"
!define MUI_FINISHPAGE_RUN "$INSTDIR\RetailCore.exe"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Для RetailCore требуется 64-разрядная Windows." /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
FunctionEnd

Section "RetailCore"
  ReadRegStr $0 HKCU "${UninstallKey}" "InstallLocation"
  ${If} $0 != ""
    ${AndIf} $0 != $INSTDIR
    ${AndIf} ${FileExists} "$0\Uninstall.exe"
    MessageBox MB_ICONSTOP "RetailCore уже установлен в $0.$\r$\nЧтобы выбрать другую папку, сначала удалите текущую установку." /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}

  ${If} $0 != $INSTDIR
    ${AndIf} ${FileExists} "$INSTDIR\Uninstall.exe"
    MessageBox MB_ICONSTOP "В выбранной папке уже есть деинсталлятор. Выберите отдельную папку для RetailCore." /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}

  ClearErrors
  SetOutPath "$INSTDIR"
  IfErrors install_error
  SetOverwrite on
  File /oname=RetailCore.exe "${RetailCoreExe}"
  IfErrors install_error
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  IfErrors install_error

  CreateShortcut "$SMPROGRAMS\RetailCore.lnk" "$INSTDIR\RetailCore.exe"
  WriteRegStr HKCU "${UninstallKey}" "DisplayName" "RetailCore"
  WriteRegStr HKCU "${UninstallKey}" "DisplayVersion" "${RetailCoreVersion}"
  WriteRegStr HKCU "${UninstallKey}" "DisplayIcon" "$INSTDIR\RetailCore.exe"
  WriteRegStr HKCU "${UninstallKey}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UninstallKey}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UninstallKey}" "NoModify" 1
  WriteRegDWORD HKCU "${UninstallKey}" "NoRepair" 1
  Return

install_error:
  MessageBox MB_ICONSTOP "Установка не завершена. Закройте RetailCore и проверьте, что выбранная папка доступна для записи." /SD IDOK
  SetErrorLevel 1
  Abort
SectionEnd

Section "Uninstall"
  ClearErrors
  Delete "$INSTDIR\RetailCore.exe"
  IfErrors uninstall_error
  Delete "$INSTDIR\RetailCore.update.exe"
  Delete "$INSTDIR\RetailCore.old.exe"
  Delete "$INSTDIR\Uninstall.exe"
  Delete "$SMPROGRAMS\RetailCore.lnk"
  DeleteRegKey HKCU "${UninstallKey}"
  RMDir "$INSTDIR"
  Return

uninstall_error:
  MessageBox MB_ICONSTOP "Закройте RetailCore и повторите удаление." /SD IDOK
  SetErrorLevel 1
  Abort
SectionEnd
