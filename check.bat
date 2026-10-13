@echo off
rem Ejecuta todas las comprobaciones de calidad (lo mismo que la integracion continua).
chcp 65001 >nul
cd /d "%~dp0"
set "PY=venv\Scripts\python.exe"
echo === ruff (estilo y errores)
"%PY%" -m ruff check . || goto :fallo
echo === mypy (tipos)
"%PY%" -m mypy local_ai_studio || goto :fallo
echo === pruebas
"%PY%" -m unittest discover -s tests || goto :fallo
echo === bandit (seguridad)
"%PY%" -m bandit -r local_ai_studio app.py -ll -q || goto :fallo
echo.
echo TODO CORRECTO
exit /b 0

:fallo
echo.
echo FALLO: revisa el mensaje de arriba.
exit /b 1
