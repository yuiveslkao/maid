@echo off
rem maid command entry. Run "maid.bat" with no arguments for help.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\maid.ps1" %*
