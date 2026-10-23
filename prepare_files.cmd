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
    ".venv\Scripts\python.exe" -B export_templates.py --date "%shipment_date%" --with-db
) else (
    ".venv\Scripts\python.exe" -B export_templates.py --with-db
)
if errorlevel 1 goto done
echo Готовые файлы находятся в папке exports, отдельно по каждому запуску.
start "" explorer.exe "%~dp0exports"
:done
pause
