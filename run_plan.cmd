@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "shipment_date="
set /p "shipment_date=Дата отгрузки ДД.ММ.ГГГГ (Enter — ближайшая): "
if not exist ".venv\Scripts\python.exe" (
    echo Сначала создайте окружение по инструкции README.md.
    pause
    exit /b 1
)
if defined shipment_date (
    ".venv\Scripts\python.exe" -B wb_declarations.py plan --date "%shipment_date%"
) else (
    ".venv\Scripts\python.exe" -B wb_declarations.py plan
)
pause
