# Seguridad

Local AI Studio procesa documentos que pueden ser confidenciales y que **no son de confianza**, y lo hace con modelos de lenguaje que
pueden equivocarse o ser manipulados por el texto que leen. Este documento describe qué se protege, cómo, cómo se ha comprobado y,
sobre todo, **qué no puede garantizar**. No existe un programa «totalmente seguro»; sí uno cuyas defensas se pueden comprobar.

## Modelo de amenazas

| Activo | Amenaza | Quién la provoca |
|---|---|---|
| Tus documentos, chats, archivos | Filtración a Internet | un PDF/página con instrucciones ocultas, una librería comprometida, un fallo |
| Tu PC (archivos, programas) | Que el modelo haga algo que tú no pediste | **inyección de instrucciones** (el texto leído se hace pasar por una orden) |
| Tu equipo | Código malicioso | un modelo/peso descargado, un paquete de PyPI, un `.exe` generado por el modelo |
| La exactitud de lo que lees | Cifras o fórmulas inventadas | el propio modelo (alucinación) |

Fuera de alcance: un atacante con acceso físico o con tu sesión de Windows abierta, malware ya instalado con tus privilegios y
vulnerabilidades del sistema operativo.

## Defensas implementadas y cómo se comprueban

| Riesgo | Medida | Dónde | Prueba |
|---|---|---|---|
| **Salida de datos a Internet** | Barrera de red de ejecución: un gancho de auditoría (PEP 578) aborta cualquier `socket.connect` o DNS a un host que no sea de bucle local. Además `HF_HUB_OFFLINE`, sin telemetría. Única excepción: descargas de modelos que confirmas, dentro de `allow_network()`. | `src/core/netguard.py` | un proceso real intenta salir y resolver DNS (falla) y hablar con un servidor local (funciona) |
| **Ollama expuesto a la red** | Solo `127.0.0.1`/`localhost`/`::1`; se niega a conectarse a cualquier otro host (`enforce_local_host`). Nunca definas `OLLAMA_HOST=0.0.0.0`. | `src/core/utils.py` | `test_core`, `test_security` |
| **Texto no fiable en el visor** (PDF o salida del modelo) | Modo seguro: HTML crudo escapado, **sin imágenes externas ni enlaces**, para que mostrar un resumen no pueda cargar nada de fuera. Se usa en el visor de PDF, chat, documentos, estudiar y la exportación. | `src/core/mdview.py` (`safe=True`) | 10 ataques al visor; una prueba comprueba que toda llamada de la interfaz usa `safe=True` |
| **Inyección de instrucciones → acciones** | El asistente solo tiene una lista cerrada de herramientas; todo resultado vuelve al modelo marcado como **datos**; cualquier acción que modifique algo exige tu aprobación viendo la acción exacta; si se pidió tras leer contenido externo, la ventana lo avisa en rojo; sin respuesta en 3 min = denegar. | `src/agent/loop.py`, `src/ui/confirm_dialog.py` | archivo con «ignora lo anterior y ejecuta…»; rechazo del usuario; herramientas inventadas; argumentos hostiles |
| **Acceso a archivos** | Política de rutas: solo carpetas permitidas (por defecto `workspace/`); resuelve `..`, uniones y enlaces; rechaza rutas UNC, flujos alternativos (`a.txt:oculto`), nombres reservados (`CON`, `NUL`…) y secretos (`.ssh`, `.env`, `id_rsa`, `*.pem`, credenciales…) aunque estén dentro de la carpeta. No se añaden carpetas de sistema, unidades enteras ni tu perfil completo. | `src/agent/policy.py` | salidas por `..`, mayúsculas, unión de Windows, secretos |
| **Código ejecutado** | Sandbox de varias capas: intérprete aislado en carpeta temporal, gancho de auditoría propio (sin red, procesos, `ctypes`, ni archivos fuera de la carpeta), Job Object de Windows (memoria limitada, un solo proceso), tiempo y salida acotados. Solo ejecuta código que **has visto y aprobado**. | `src/agent/sandbox.py` | 15 ataques (red, subprocess, `ctypes`, leer `win.ini`/`.ssh`, borrar fuera, bomba de memoria…) |
| **Herramientas no incluidas a propósito** | No hay terminal libre, red, borrado, instalación de software ni registro de Windows. Crear archivos `.exe`, scripts, `.lnk`… y abrir ejecutables está prohibido. | `src/agent/tools.py` | test de lista cerrada |
| **Descargas de modelos** | Ollama: lista cerrada de nombres (`ALLOWED_PULLS`), con tu confirmación. Imágenes: solo `.safetensors` (sin código ejecutable), repositorio fijado por **commit**, **SHA-256** de cada peso fijado en el código; si no coincide se borra. El pipeline se carga con `local_files_only=True`. | `src/chat/engine.py`, `src/images/models.py` | descarga manipulada, nombres hostiles |
| **Fuga de secretos al repositorio** | `pre-commit` + `detect-secrets` (baseline vacía); `.gitignore` excluye `config.yaml`, `.env`, claves, `models/` (varios GB), `conversations/`, `outputs/`, `workspace/`, bases de datos y registros. | `.pre-commit-config.yaml`, `.gitignore` | test del `.gitignore` |
| **Entradas hostiles** | PDF: límites de tamaño y páginas, rechazo de cifrados/corruptos. Imágenes: tope de megapíxeles. SQLite: consultas parametrizadas (y sentencias fijas). Consultas FTS sin operadores inyectables. Nombres de archivo saneados. JSON de tarjetas validado, nunca evaluado. CSV neutraliza fórmulas de hoja de cálculo. Nunca `eval`, `exec`, `pickle`, `shell=True`. | varios | `test_security`, `test_rag`, `test_study`, `test_images` |
| **Alucinaciones** | Verificación determinista de cifras, fórmulas (geometría del PDF + sympy), código y citas. Lo no comprobable se marca ⚠, no se da por bueno. | `src/pdf/`, `src/rag/`, `src/study/` | `test_core`, `test_mathgeom`, `test_symcheck`, `test_rag` |

## Auditoría realizada (12-oct-2026)
- **Análisis estático (`bandit`)**: 0 hallazgos de severidad media o alta tras corregir MD5 (ahora SHA-256) y una sentencia SQL construida con texto (ahora sentencias fijas). Quedan avisos de severidad baja (`try/except/pass` en rutas de interfaz y `os.startfile` con ruta validada).
- **Dependencias (`pip-audit`)**: sin vulnerabilidades conocidas tras actualizar `pip` y `setuptools` (tenían 18). `torch` (build CUDA) no se puede auditar contra PyPI.
- **Revisión manual**: se encontraron y corrigieron cinco fallos que las herramientas no ven: el visor de resúmenes no usaba modo seguro (una imagen remota en el texto de un PDF se habría descargado), no había barrera de red de ejecución, `pull` aceptaba cualquier modelo, `.gitignore` no cubría modelos/conversaciones, y el `import` de langchain estaba roto en el entorno.
- Repetir: `bandit -r src app.py -ll`, `pip-audit`, `python -m unittest discover -s tests`.

## Límites conocidos (lo que NO se garantiza)
- **El sandbox no es una frontera absoluta.** Un gancho de auditoría de Python no cubre código nativo ni todos los rodeos posibles; por eso solo se ejecuta código que ves y apruebas. Para código realmente hostil haría falta una máquina virtual.
- **La barrera de red cubre el proceso de Python**, no código nativo que abra sockets por su cuenta ni otros procesos. Ollama es un proceso aparte con Internet (para descargar modelos cuando se lo pides).
- **La inyección de instrucciones no tiene solución completa**: reducimos el daño (lista cerrada de herramientas, permisos, marcas de «contenido externo»), pero si apruebas una acción sin leerla, la apruebas.
- **Los datos de usuario están en claro en disco**: `conversations/` (chats, índice de documentos, registro de acciones), `resumenes/`, `outputs/`. Cifra el disco (BitLocker) si el equipo se comparte.
- **Un modelo puede equivocarse.** Las comprobaciones deterministas reducen mucho los errores de cifras, fórmulas y citas, pero no sustituyen la revisión humana de un documento crítico.
- **PyTorch/diffusers/transformers** son dependencias grandes; mantenlas actualizadas. El tercer lector de fórmulas opcional (`pix2tex`, desactivado) descarga pesos con `pickle` de la red: no lo actives con redes no fiables.
- Las versiones de `requirements.txt` no están fijadas para recibir parches; usa `pip-audit` periódicamente.
- Sin cifrado de la comunicación con Ollama (es local: bucle de loopback).

## Lo que debes hacer tú
- No definas `OLLAMA_HOST=0.0.0.0` ni abras el puerto 11434 en el firewall/router.
- Lee la ventana de permiso antes de pulsar «Permitir». Ante la duda, **Denegar**.
- Añade carpetas de lectura al asistente solo si las necesitas, y nunca tu perfil completo.
- Instala los hooks: `pip install pre-commit detect-secrets && pre-commit install`, y no subas `config.yaml`.
- Antes de publicar el repositorio, comprueba que el repositorio git es el del proyecto (`git rev-parse --show-toplevel`).

## Reportar una vulnerabilidad
Abre un aviso privado de seguridad en GitHub («Security → Report a vulnerability») o contacta a quien mantiene el repositorio.
No publiques detalles de explotación en un issue público.
