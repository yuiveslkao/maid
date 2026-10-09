@echo off
rem Double-click launcher. See scripts\start.ps1 for options.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
pause
