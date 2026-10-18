@echo off
rem Double-click to open the launcher (choose chat-only or full). Same as "maid.bat launch".
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\maid.ps1" launch %*
pause
