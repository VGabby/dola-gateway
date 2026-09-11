@echo off
if not defined LOCALAPPDATA exit /b 1
if not exist "%LOCALAPPDATA%\DolaGateway" mkdir "%LOCALAPPDATA%\DolaGateway"
explorer.exe "%LOCALAPPDATA%\DolaGateway"
