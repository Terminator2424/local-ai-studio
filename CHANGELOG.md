# Historial de cambios

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y versionado semántico.

## [1.0.0] — 2026-10-12

Primera versión pública: la aplicación pasó de «resumidor de PDF» a un estudio de IA local completo y se sometió a una auditoría
técnica ([docs/AUDITORIA.md](docs/AUDITORIA.md)).

### Añadido
- **Local AI Studio** unificado: Resumir PDF, Documentos, Chat, Estudiar, Generar imagen, Editar imagen y Ajustes.
- **Fórmulas reconstruidas desde la geometría del PDF** (sin modelo), doble lectura con visión y verificación con sympy.
- **Documentos**: búsqueda híbrida (FTS5 + embeddings), respuestas con página citada, comprobación de citas y cifras, y **casilla por
  documento** para elegir en cuáles preguntar.
- **Chat**: 6 modelos con descripción en el selector (Llama 3.1 8B por defecto), **roles** (general, tutor de ingeniería, programador,
  seguridad defensiva, redactor académico) y asistente con **herramientas** con permisos y sandbox.
- **Ajustes**: panel de almacenamiento con limpieza segura y poda automática, estado de la barrera de red.
- `python -m local_ai_studio` con `--doctor` y `--version`; `LOCAL_AI_STUDIO_HOME` para mover todos los datos.
- Integración continua, `pyproject.toml` (ruff, mypy), `requirements` separados (ejecución, imágenes, desarrollo), licencia MIT.
- **Un solo trabajo pesado a la vez en la GPU**: si ya hay uno en curso (resumir, generar imagen, chat, indexar…), el segundo se niega con
  un aviso claro en lugar de pisarle los modelos en la VRAM; la barra de estado muestra «Trabajando: …».
- **Primeros pasos**: comprobación de Ollama, modelos y CUDA con el comando exacto para arreglar cada carencia (aviso al abrir, tarjeta en
  Ajustes y `--doctor`); `instalar.bat` para instalar con doble clic.
- Tarjetas de estudio: exportación a **Anki (importación directa)**, **Notion (CSV)** y **Markdown**.
- **Historial de versiones** de los resúmenes (`resumenes/historial/`, últimas 10) y **modelo recordado por tarea** (Documentos, Estudiar).
- Documentos: la respuesta indica de qué documento sale cada dato y avisa de contradicciones; la cita literal se centra en la frase que respalda la respuesta.
- `CODE_OF_CONDUCT.md`, contacto de seguridad real y una demo en GIF.
- 352 pruebas, incluidas las de arquitectura, higiene del repositorio y una prueba de humo de la interfaz real.

### Cambiado
- Paquete `src` → **`local_ai_studio`**; `core/utils.py` dividido en módulos de una sola responsabilidad; capas sin ciclos.
- «Documentos» abre en 0,4 s (antes 10,5 s): `127.0.0.1` en lugar de `localhost` y un cliente de Ollama compartido.
- La cabecera del chat se reorganizó para que nada quede cortado con anchos reales de ventana.
- `extract_pages`, `mathgeom._layout`/`page_formulas`, `Summarizer.process`/`_synthesize`, `decide` y `build_chunks` divididos en pasos pequeños
  (resultado idéntico, comprobado con miles de entradas contra la versión anterior).
- El registro va a `logs/app.log` y se configura al arrancar.

### Corregido
- Regex con carácter de control (`\b` guardado como backspace) que rompía en silencio la detección de código; y una aserción de
  seguridad que no podía fallar por el mismo motivo.
- Una tecla (Enter/Ctrl+Enter) o un clic ya no cancelan una respuesta o un indexado en curso.
- El extractor cierra el PDF y pdfplumber al cancelar o fallar.
- Índice de documentos transaccional; filtro por documento aplicado antes del ranking; vectores de distinta dimensión.
- `latex_to_text` ya no falla con una fórmula que termina en `\`.
- Configuración incompleta o vacía ya no impide arrancar.
- Reintentos a Ollama: no se reintentan errores permanentes ni se duerme tras el último intento.
- Bomba de descompresión: el tamaño de la imagen se comprueba en la cabecera antes de decodificar.

### Seguridad
- La aplicación **ya no instala paquetes** al arrancar: avisa y muestra el comando.
- Barrera de red visible en Ajustes; `bandit` y `pip-audit` sin hallazgos.

### Eliminado
- Código muerto (funciones sin uso, una opción de configuración) y las dependencias `pypdf`, `tqdm`, `rich` y `torchvision`.
