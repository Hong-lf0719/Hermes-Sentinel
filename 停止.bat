@echo off
cd /d "%~dp0"
echo. > hermes.stop
echo 已发送停止信号，Hermes 将在数秒内关闭。
timeout /t 3 >nul
