@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist ".venv\Scripts\python.exe" (
  echo [FATAL] .venv not found. Run: python -m venv .venv ^& .venv\Scripts\pip install -r requirements.txt
  exit /b 2
)
call .venv\Scripts\activate.bat
python scripts\env_check.py
if errorlevel 1 (
  echo [FATAL] environment check failed
  exit /b 3
)
python scripts\batch_convert.py
set RC=%errorlevel%
if %RC%==3 (
  echo [FATAL] conversion aborted
  exit /b 3
)
python scripts\analyze_dxf.py
python scripts\find_scada.py
python scripts\summary.py
echo Done. batch return code=%RC%
exit /b %RC%
