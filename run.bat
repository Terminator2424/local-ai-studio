@echo off
chcp 65001 >nul
cd /d "%~dp0"
call venv\Scripts\activate.bat
python app.py
if errorlevel 1 (
    echo.
    echo La aplicacion termino con errores. Revisa resumenes\app.log
    pause
)
