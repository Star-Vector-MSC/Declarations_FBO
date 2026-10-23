@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -B -u telegram_bot.py
pause
