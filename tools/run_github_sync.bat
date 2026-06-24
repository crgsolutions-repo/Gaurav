@echo off
setlocal
cd /d "%~dp0.."
venv\Scripts\python.exe tools\github_sync.py
echo.
pause
