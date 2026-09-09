@echo off
setlocal
title WaveScout Build

echo.
echo  ============================================================
echo   WaveScout portable build
echo  ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Install Python 3.11 from https://python.org
    pause
    exit /b 1
)

echo  Installing build dependencies...
python -m pip install --quiet --upgrade -r "%~dp0requirements.txt" -r "%~dp0requirements-build.txt"
if errorlevel 1 (
    echo  [ERROR] Dependency install failed.
    pause
    exit /b 1
)

python -B "%~dp0build.py"
if errorlevel 1 (
    echo.
    echo  [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo  Build complete.
pause
