@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запуск CS2 Movie Maker...
python -X utf8 app.py
pause
