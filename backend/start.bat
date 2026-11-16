@echo off
cd /d "%~dp0"
title Bindswift server

rem First run on a new computer: build the Python environment.
if not exist ".venv\Scripts\python.exe" (
  echo Setting up Python for Bindswift. This only happens once and takes a few minutes...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if not exist ".venv\Scripts\python.exe" (
    echo Could not create the Python environment. Install Python 3.11+ from python.org, tick "Add to PATH", then run this again.
    pause
    exit /b 1
  )
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)

if not exist ".env" (
  echo No .env file found in %CD%
  echo Copy your .env from your desktop PC into this folder, then run this again.
  pause
  exit /b 1
)

echo Starting Bindswift at http://localhost:8000  (admin: http://localhost:8000/admin)
echo Press Ctrl+C to stop. Close and double-click again after changing .env
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
pause
