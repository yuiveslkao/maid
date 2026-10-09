@echo off
rem Double-click launcher. Same as "maid.bat start".
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\maid.ps1" start %*
pause
