@echo off
setlocal
set "state_dir=%LOCALAPPDATA%\DolaGateway"
set "runtime_python=%state_dir%\runtime\venv\Scripts\python.exe"

if not defined LOCALAPPDATA goto failed
if exist "%runtime_python%" if exist "%state_dir%\.env.local" if exist "%state_dir%\runtime\browsers" goto run

echo First launch: setting up Dola Gateway for this Windows account.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\setup.ps1"
if errorlevel 1 goto failed

:run
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\run.ps1"
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo Dola Gateway could not start. Run doctor.cmd for details.
pause
exit /b 1
