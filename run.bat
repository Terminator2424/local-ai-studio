@echo off
rem Abre Local AI Studio. Usa el Python del entorno virtual directamente (sin activate.bat: ese guarda una ruta
rem absoluta y deja de funcionar si mueves o renombras la carpeta del proyecto).
chcp 65001 >nul
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
    echo No existe el entorno virtual. Crealo una vez con:
    echo   python -m venv venv
    echo   venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
"venv\Scripts\python.exe" -m local_ai_studio %*
if errorlevel 1 (
    echo.
    echo La aplicacion termino con errores. Revisa logs\app.log
    pause
)
