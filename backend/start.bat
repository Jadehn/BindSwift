@echo off
cd /d "%~dp0"
title Bindswift server

rem Each computer keeps its own Python environment OUTSIDE OneDrive, so a laptop and a desktop
rem never overwrite each others. It lives in %LOCALAPPDATA%\Bindswift\venv.
set "VENV=%LOCALAPPDATA%\Bindswift\venv"
set "PY=%VENV%\Scripts\python.exe"

if exist "%PY%" "%PY%" -c "import sys" >nul 2>nul || rmdir /s /q "%VENV%" 2>nul
if not exist "%PY%" (
  echo Setting up Python for Bindswift on this computer. This only happens once and takes a few minutes...
  where py >nul 2>nul && (py -3 -m venv "%VENV%") || (python -m venv "%VENV%")
  if not exist "%PY%" (
    echo Could not create the Python environment. Install Python 3.11+ from python.org, tick "Add to PATH", then run this again.
    pause
    exit /b 1
  )
  "%PY%" -m pip install --upgrade pip
)

rem Install or update packages whenever requirements.txt changes.
fc /b requirements.txt "%VENV%\requirements.installed" >nul 2>nul || (
  "%PY%" -m pip install -r requirements.txt && copy /y requirements.txt "%VENV%\requirements.installed" >nul
)

if not exist ".env" (
  echo No .env file found in %CD%
  echo Copy .env.example to .env and fill it in.
  pause
  exit /b 1
)

echo Starting Bindswift at http://localhost:8000  (admin: http://localhost:8000/admin)
echo Press Ctrl+C to stop. Close and double-click again after changing .env
"%PY%" -m uvicorn app.main:app --reload --reload-dir app
pause
