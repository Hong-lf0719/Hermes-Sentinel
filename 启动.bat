@echo off
cd /d "%~dp0"
echo ==========================================
echo        Hermes Sentinel
echo ==========================================
echo.
echo   Web UI:  http://127.0.0.1:9866
echo.
echo   Starting on port 9866...
echo ==========================================
echo.

D:\Anaconda\python.exe server.py
pause
