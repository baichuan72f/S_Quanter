@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal enabledelayedexpansion
set "PY=.venv\Scripts\python.exe"

echo [1/3] 检查内置虚拟环境 ...
if exist "%PY%" (
  "%PY%" -c "import pandas, numpy, duckdb" >nul 2>&1
  if !errorlevel! equ 0 goto ok
)

echo [2/3] 内置环境无法启动（多半是打包机器上的 Python 路径不同），尝试自动修复 ...
set "BASEPY="
for /f "delims=" %%i in ('py -3.11 -c "import sys;print(sys.base_prefix)" 2^>nul') do set "BASEPY=%%i"
if not defined BASEPY (
  for /f "delims=" %%i in ('python -c "import sys;print(sys.base_prefix) if sys.version_info[:2]==(3,11) else sys.exit(1)" 2^>nul') do set "BASEPY=%%i"
)
if not defined BASEPY goto rebuild

if not exist "%PY%" goto rebuild
(
  echo home = !BASEPY!
  echo include-system-site-packages = false
  echo version = 3.11.9
  echo prompt = smallquant
) > ".venv\pyvenv.cfg"
"%PY%" -c "import pandas, numpy, duckdb" >nul 2>&1
if !errorlevel! equ 0 goto ok
goto rebuild

:rebuild
echo [3/3] 未找到可用的 Python 3.11，改为新建虚拟环境并安装依赖（需要联网）...
py -3.11 -m venv .venv 2>nul || python -m venv .venv
if not exist "%PY%" (
  echo [失败] 本机没有 Python 3.11。请先安装 Python 3.11（勾选 Add to PATH），再重新运行本文件。
  pause
  exit /b 1
)
"%PY%" -m pip install --upgrade pip >nul 2>&1
"%PY%" -m pip install -r requirements.txt
if !errorlevel! neq 0 (
  echo [失败] 依赖安装失败，请检查网络后重试。
  pause
  exit /b 1
)

:ok
echo.
echo 环境就绪：
"%PY%" -c "import sys, pandas, numpy, duckdb; print('  Python', sys.version.split()[0], '| pandas', pandas.__version__, '| numpy', numpy.__version__, '| duckdb', duckdb.__version__)"
echo.
echo 下一步：双击「启动看板.bat」，或在命令行运行
echo   .venv\Scripts\python.exe run_daily.py --strategy strategies\小市值5日轮动2.ipynb --status
pause
