@echo off
rem Double-click to open the launcher, then start. Same as "maid.bat start".
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\maid.ps1" start %*
pause
