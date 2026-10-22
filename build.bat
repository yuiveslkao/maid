@echo off
rem Build maid.exe on this PC. The .NET SDK goes into runtime\dotnet (no installer).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build.ps1"
pause
