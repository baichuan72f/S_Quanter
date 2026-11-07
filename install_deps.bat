@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal
set "PY=.venv\Scripts\python.exe"
set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"

echo ============================================================
echo  SmallQuant: install extra dependencies for cloned strategies
echo  (scipy / sklearn / talib / torch / optuna / ... )
echo  The bundled .venv only ships pandas / numpy / duckdb.
echo ============================================================
echo.

if not exist "%PY%" (
  echo [ERROR] .venv not found. Run the env-repair script first.
  pause
  exit /b 1
)

echo [1/4] pip ...
"%PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
  echo       pip missing, bootstrapping with ensurepip ...
  "%PY%" -m ensurepip --upgrade
  if errorlevel 1 (
    echo [ERROR] ensurepip failed.
    pause
    exit /b 1
  )
)

echo [2/4] audit current state ...
"%PY%" check_deps.py --missing
if not errorlevel 1 (
  echo       nothing missing, skip install.
  goto verify
)

echo [3/4] install requirements-extra.txt  (network, ~600 MB) ...
"%PY%" -m pip install -r requirements-extra.txt -c constraints.txt -i %MIRROR% --timeout 90
if errorlevel 1 (
  echo.
  echo [FAILED] install did not finish. Common causes:
  echo   - mirror unreachable: retry with the default index
  echo       "%PY%" -m pip install -r requirements-extra.txt -c constraints.txt
  echo   - a package has no wheel for Python 3.11: read the error line above
  pause
  exit /b 1
)

:verify
echo [4/4] verify ...
"%PY%" check_deps.py --missing
if errorlevel 1 (
  echo.
  echo [WARN] some modules are still missing - see the list above.
  echo        Requirements deliberately not installed: tensorflow / keras /
  echo        zipline / rqalpha / vnpy / pandas-ta ^(needs Python 3.12^).
  pause
  exit /b 1
)

echo.
echo All strategy dependencies present.
pause
