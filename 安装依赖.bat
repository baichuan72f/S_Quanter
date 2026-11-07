@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal
set "PY=.venv\Scripts\python.exe"

echo ============================================================
echo  安装克隆策略常用的扩展依赖（scipy / sklearn / matplotlib 等）
echo  随包 .venv 只带 pandas / numpy / duckdb，缺 scipy 等会报
echo  ModuleNotFoundError: No module named 'scipy'
echo ============================================================
echo.

if not exist "%PY%" (
  echo [错误] 没有找到 .venv，请先双击「修复环境.bat」。
  pause
  exit /b 1
)

echo [1/3] 检查 pip ...
"%PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
  echo        .venv 里没有 pip，正在安装 ...
  "%PY%" -m ensurepip --upgrade
)

echo [2/3] 安装 requirements-extra.txt（走清华镜像，约 300 MB，需要联网）...
"%PY%" -m pip install -r requirements-extra.txt -c constraints.txt ^
        -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 60
if errorlevel 1 (
  echo.
  echo [失败] 安装没成功。常见原因：网络不通 / 镜像不可用。
  echo        可换官方源重试：
  echo        "%PY%" -m pip install -r requirements-extra.txt -c constraints.txt
  pause
  exit /b 1
)

echo [3/3] 验证关键包 ...
"%PY%" -c "import scipy, sklearn, statsmodels, matplotlib; print('  OK  scipy', scipy.__version__, '| sklearn', sklearn.__version__, '| statsmodels', statsmodels.__version__, '| matplotlib', matplotlib.__version__)"
if errorlevel 1 (
  echo [失败] 有包导入失败，请看上面的报错。
  pause
  exit /b 1
)

echo.
echo 依赖就绪，可以跑克隆策略了。
pause
