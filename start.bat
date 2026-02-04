@echo off
setlocal
set "ROOT_DIR=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT_DIR%start.ps1"

endlocal
