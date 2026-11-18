@echo off
setlocal
cd /d "%~dp0"
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Projeye ait .venv bulunamadi. README kurulum adimlarini uygulayin.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0tests\manual_demo.py"
if errorlevel 1 (
    echo Demo hata ile kapandi. Yukaridaki hata mesajini kontrol edin.
    pause
    exit /b 1
)
