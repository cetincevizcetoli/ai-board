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
"%~dp0.venv\Scripts\python.exe" -m unittest discover -s tests -v
set "TEST_RESULT=%ERRORLEVEL%"
pause
exit /b %TEST_RESULT%
