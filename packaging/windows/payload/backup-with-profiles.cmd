@echo off
setlocal
echo WARNING: This backup will contain reusable browser login sessions.
echo Store it securely and never send it to anyone.
echo.
pause
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\backup.ps1" -IncludeProfiles
echo.
pause
