@echo off
setlocal
cd /d "%~dp0"

title SCADA DWG Analyzer - Debug

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python virtual environment not found.
    echo Expected: %CD%\.venv\Scripts\python.exe
    pause
    exit /b 1
)

echo Starting SCADA DWG Analyzer...
echo.

".venv\Scripts\python.exe" "app.py"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo [Exit code: %EXIT_CODE%]
echo Press any key to close...
pause >nul

exit /b %EXIT_CODE%
