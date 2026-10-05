@echo off
rem After the pipelines: merge into Waifu, names agent, small titles to Other, numbering.
rem Keep this file ASCII-only: cmd reads it before switching the code page.
chcp 65001 >nul
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
    echo [ERROR] venv not found in %~dp0
    echo         Fix: py -3.12 -m venv venv  then  venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
"venv\Scripts\python.exe" "tools\finish.py" %*
