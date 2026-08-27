@echo off
REM Double-click this file to start the translator (loads local_config.ps1 once configured)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_translator.ps1"
if errorlevel 1 pause
