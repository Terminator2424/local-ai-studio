@echo off
rem Instala Local AI Studio: entorno virtual + dependencias, y al final comprueba que todo lo demas (Ollama, modelos, GPU) esta listo.
rem No instala nada fuera de esta carpeta. Para generar imagenes (opcional) mira requirements-images.txt.
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo === Local AI Studio: instalacion ===
echo.

where python >nul 2>nul
if errorlevel 1 goto :sin_python
python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 12) else 1)"
if errorlevel 1 goto :python_viejo

if exist "venv\Scripts\python.exe" goto :tiene_venv
echo [1/3] Creando el entorno virtual...
python -m venv venv
if errorlevel 1 goto :fallo
goto :deps
:tiene_venv
echo [1/3] El entorno virtual ya existe.

:deps
echo [2/3] Instalando dependencias (puede tardar unos minutos)...
"venv\Scripts\python.exe" -m pip install --upgrade pip
"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fallo

echo.
echo [3/3] Comprobando el equipo...
echo.
"venv\Scripts\python.exe" -m local_ai_studio --doctor
echo.
where ollama >nul 2>nul
if errorlevel 1 echo AVISO: no encuentro Ollama. Descargalo de https://ollama.com e instala los modelos que indica la lista de arriba.
echo.
echo Listo. Abre la aplicacion con run.bat
echo (si arriba hay lineas con una cruz, haz primero lo que dicen: son los modelos que faltan).
echo.
pause
exit /b 0

:sin_python
echo No encuentro Python. Instala Python 3.12 desde https://www.python.org
echo y marca la casilla "Add python.exe to PATH" durante la instalacion. Despues vuelve a ejecutar este archivo.
echo.
pause
exit /b 1

:python_viejo
echo Hace falta Python 3.12 o superior. Tienes:
python --version
echo Instala una version reciente desde https://www.python.org y vuelve a ejecutar este archivo.
echo.
pause
exit /b 1

:fallo
echo.
echo La instalacion fallo. Lee el mensaje de arriba (suele ser falta de conexion para descargar los paquetes).
echo.
pause
exit /b 1
