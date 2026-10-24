@echo off
chcp 65001 >nul
cd /d D:\new\ai_friend
call .venv\Scripts\activate.bat
start "" /b .venv\Scripts\python.exe open_browser_when_ready.py
python -m app.main
pause
