@echo off
cd /d "%~dp0"
title Bindswift server
echo Starting Bindswift at http://localhost:8000  (admin: http://localhost:8000/admin)
echo Press Ctrl+C to stop. Close and double-click again after changing .env
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
pause
