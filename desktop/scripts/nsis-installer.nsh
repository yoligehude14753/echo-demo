!macro customInit
  ; Repair stale current-user installs before electron-builder tries to run an
  ; older uninstaller. User data lives outside $INSTDIR and is intentionally kept.
  ${If} ${Silent}
    ExecWait 'taskkill /IM EchoDesk.exe /F'
    DeleteRegKey HKCU "${UNINSTALL_REGISTRY_KEY}\"
    DeleteRegKey HKCU "${INSTALL_REGISTRY_KEY}\"
    ${If} ${FileExists} "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
      RMDir /r "$INSTDIR"
    ${EndIf}
  ${EndIf}
!macroend
