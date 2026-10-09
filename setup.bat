@echo off
rem Double-click to download everything into the runtime folder.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\maid.ps1" setup %*
pause
