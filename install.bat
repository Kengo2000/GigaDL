@echo off
rem GigaDL installer for Windows - double-click this file
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
