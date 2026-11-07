@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [错误] 没有找到 .venv，请先双击「修复环境.bat」。
  pause
  exit /b 1
)
echo 正在启动本地看板 http://127.0.0.1:8765/ ...（关闭本窗口即停止）
".venv\Scripts\python.exe" dashboard.py %*
pause
