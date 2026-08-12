@echo off
cd /d "%~dp0"
if exist hermes.stop del /q hermes.stop
start "" /min D:\Anaconda\pythonw.exe watchdog.py
echo ==========================================
echo   Hermes Sentinel 已在后台启动
echo   - 无窗口常驻，关闭本窗口不影响服务
echo   - 崩溃自动重启
echo   - 日志：hermes.log
echo   - 停止：运行 停止.bat
echo   Web UI: http://127.0.0.1:9866
echo ==========================================
timeout /t 3 >nul
