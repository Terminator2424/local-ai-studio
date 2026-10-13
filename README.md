# Local AI Studio

Asistente de IA **100 % local** para Windows: resume PDFs con fórmulas y código sin inventar nada, conversa, responde sobre tus
propios documentos con citas de página, ayuda a estudiar, genera y edita imágenes, y puede ejecutar acciones sencillas en tu PC
**solo con tu permiso**. Nada de lo que escribes ni de tus documentos sale del equipo.

Pensado para una RTX 4060 de 8 GB (se adapta a tu hardware). Interfaz simple: fondo blanco, modo oscuro opcional, acento rojo.

| Sección | Qué hace |
|---|---|
| **Resumir PDF** | PDF → Markdown fiel (código, tablas, fórmulas LaTeX) con verificación y exportación a `.md`, `.pdf` y `.docx`. Cola de carpetas con reanudación. |
| **Documentos** | Pregunta a tus PDFs: búsqueda híbrida (palabras + sentido), respuestas con página citada y comprobación de cifras. |
| **Chat** | Conversación con historial (SQLite), 6 modelos, parámetros, imágenes para el modelo de visión, Markdown + LaTeX. Interruptor **Herramientas** (ver abajo). |
| **Estudiar** | Tarjetas (CSV para Anki), preguntas de práctica y ejercicios paso a paso con cada igualdad marcada ✓ / ✗ / ⚠. Ejecuta el código en un sandbox. |
| **Generar imagen** | Stable Diffusion 1.5 y SDXL en fp16, lote 1–4, semilla, guardar/copiar/abrir carpeta. |
| **Editar imagen** | Recorte, giros, tamaño y filtros con vista previa; guarda PNG, JPEG o WebP. |
| **Ajustes** | Diagnóstico de GPU/VRAM/PyTorch, modelos instalados (descarga con confirmación) y liberar VRAM. |

## Instalación (Windows 10/11)
1. Instala [Ollama](https://ollama.com) y [Python 3.12](https://www.python.org).
2. Crea el entorno e instala las dependencias:
   ```bash
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. **Solo para generar imágenes** instala PyTorch con CUDA (la versión por defecto de PyPI es la de CPU) y las librerías de difusión:
   ```bash
   pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
   pip install diffusers accelerate
   ```
   Comprueba en **Ajustes** que PyTorch muestra «con CUDA». Elige el `cuXXX` que corresponda a tu driver en [pytorch.org](https://pytorch.org).
4. Abre la app con doble clic en `run.bat` (o `python app.py`). La primera vez crea `config.yaml` desde `config.example.yaml`.
5. Descarga los modelos desde la propia app (**Ajustes**, con confirmación) o a mano:
   ```bash
   ollama pull qwen2.5:7b
   ollama pull qwen2.5vl:7b
   ollama pull nomic-embed-text
   ```
   Opcionales: `llama3.1:8b`, `gemma2:9b`, `mistral:7b`, `qwen2.5-coder:7b`. Los modelos de imagen se descargan desde **Generar imagen**.

## La VRAM: un modelo a la vez
Con 8 GB no caben a la vez un modelo de chat y uno de imágenes. `src/core/model_manager.py` decide qué está cargado: antes de generar
imágenes descarga los modelos de Ollama, y antes de chatear libera el pipeline de imágenes. La barra inferior muestra qué hay en memoria y
la VRAM libre; al cambiar de función hay una espera de unos segundos mientras se carga el otro modelo.

## Seguridad en una línea
Barrera de red que bloquea cualquier conexión que no sea a `localhost`, visor sin imágenes externas ni enlaces, descargas solo de listas
cerradas y con hash, y un asistente de acciones que **nunca** escribe, ejecuta ni abre nada sin enseñarte la acción exacta.
Detalle, modelo de amenazas y límites honestos en [SECURITY.md](SECURITY.md).

## Asistente con herramientas (acciones en el PC)
En **Chat**, el interruptor **Herramientas** permite al modelo (Qwen 2.5, Llama 3.1, Mistral) pedir acciones:

| Herramienta | Permiso |
|---|---|
| listar carpeta, leer archivo (texto/PDF), buscar archivos, calcular, fecha y hora | automático, solo dentro de las carpetas permitidas |
| crear archivo de texto, ejecutar Python aislado, abrir un documento o imagen | **te pide permiso cada vez**, mostrando la acción exacta |

Por defecto solo trabaja en la carpeta `workspace/`. Puedes añadir carpetas de **lectura**. No hay terminal libre, ni red, ni borrado, ni
instalación, ni acceso a claves o a carpetas del sistema. Si el modelo lee un archivo con instrucciones ocultas, la aplicación lo marca
en la ventana de permiso. Todo queda en `conversations/actions.log`.

## Resumir PDF: cómo se garantiza la fidelidad
Un modelo de 7B alucina si reescribe mucho texto técnico. Por eso en el nivel por defecto, **Texto limpio**, el contenido **no lo reescribe
ningún modelo**: se extrae y se comprueba.
- **Fórmulas reconstruidas desde el propio PDF** (`src/pdf/mathgeom.py`): un PDF de LaTeX guarda cada símbolo con su fuente, tamaño y línea
  base, y las fracciones son reglas. Con eso se reconstruye el LaTeX sin modelo (fracciones, subíndices, integrales y sumatorios con límites).
  Si algo no se puede explicar, el bloque se deja a la lectura de la visión. Cuando ambas lecturas discrepan, la fórmula sale con ⚠ y dice
  **qué símbolo** difiere («PDF «3» · visión «2»»).
- **Doble lectura de la visión** (200 y 300 dpi), **cifras** de cada fórmula presentes en el PDF, **cadenas numéricas** recalculadas sin `eval`
  y **verificación simbólica** con sympy (`(x+1)^2 = x^2+1` se marca; `A = 36` o `F(1)` se dejan sin opinar).
- **Código**: cada línea debe existir en el PDF, los delimitadores deben cuadrar y, en Python, compilar.
- **Informe de fidelidad** al final: «fórmulas verificadas 96/101 · ⚠ 2 a revisar (págs. 3, 9)».

Los niveles con IA (Compacto IA, Normal, Detallado) reescriben y por eso se sanean con `src/pdf/scrub.py`; úsalos solo como secundarios.
Detalle de cada nivel y del ahorro de tokens: **Texto limpio** ≈ 90 % menos tokens que enviar el PDF; un resumen con IA solo compensa en
documentos largos de mucha prosa.

## Pruebas
```bash
python -m unittest discover -s tests -v
```
Más de 220 pruebas, ninguna necesita Ollama (el cliente se simula). Cubren extracción y verificación de PDFs, geometría de fórmulas,
chat y almacén, gestor de VRAM, imágenes, RAG, modo estudiar, exportación a Word, el asistente y su sandbox, y la seguridad (barrera de red
con un proceso real, ataques al visor, políticas de rutas, listas cerradas de descargas y patrones peligrosos en el código).
La prueba de GPU real y las que usan el PDF de Simpson solo corren si el modelo o el archivo están en tu equipo.

Auditoría de seguridad (opcional): `pip install bandit pip-audit` y luego `bandit -r src app.py -ll` y `pip-audit`.

## Hardware: ajuste automático
`src/core/hardware_detector.py` detecta GPU/VRAM (NVML o `nvidia-smi`), RAM y GPU integrada y ajusta, sin tocar tu `config.yaml`:

| VRAM libre | Modelo | `num_ctx` |
|---|---|---|
| ≥ 12 GB | `qwen2.5:14b` (si está instalado) | 8192–16384 |
| 8–12 GB | `qwen2.5:7b` | 16384 |
| 6–8 GB | `qwen2.5:7b` | 8192 |
| 4–6 GB | `qwen2.5:7b` | 4096 |
| < 4 GB / sin GPU NVIDIA | `qwen2.5:7b` (o `3b`) en CPU | 2048–4096 |

Nota: la VRAM libre real es menor que la total porque Windows usa parte para el escritorio (en una RTX 4060 de 8 GB suelen quedar ~6–7 GB).
`python -m src.core.hardware_detector` imprime el diagnóstico.

## Estructura
```
app.py                       punto de entrada (ventana unificada)
run.bat                      lanzador con doble clic
config.example.yaml          plantilla de configuración y prompts (config.yaml es local)
src/core/                    utils, hardware, caché, gestor de VRAM, barrera de red, visor Markdown, exportación a Word
src/pdf/                     extracción, mathgeom (fórmulas desde glifos), symcheck (sympy), verificación, resumidor
src/chat/                    historial SQLite, motor de Ollama, renderizado seguro
src/agent/                   política de rutas, herramientas, bucle con permisos, sandbox
src/rag/                     índice híbrido de documentos y comprobación de respuestas
src/study/                   tarjetas y verificación de pasos
src/images/                  catálogo y descarga verificada, generación (diffusers), edición (Pillow), portapapeles
src/ui/                      ventana principal y páginas
tests/                       pruebas
```
Datos de usuario (fuera de git): `resumenes/`, `imagenes/`, `conversations/` (chats, índice, registro de acciones), `outputs/`, `models/`, `workspace/`.

## Preguntas frecuentes
**¿Tiene restricciones éticas, o hay que entrenarla?** Las restricciones y el conocimiento están en los pesos del modelo, fruto de su entrenamiento
(Qwen, Llama, Mistral, Gemma); esta aplicación no los modifica. Su conocimiento acaba en su fecha de entrenamiento y puede equivocarse: por eso
la app comprueba con código lo que se puede comprobar. No hace falta entrenar nada para usar tus documentos: eso lo cubre **Documentos**
(RAG). Entrenar (fine-tuning con LoRA) solo sirve para cambiar estilo o formato y requiere datos y horas de GPU.

**¿Puede controlar todo el PC?** No, y es deliberado. Ver «Asistente con herramientas» y [SECURITY.md](SECURITY.md).

**Licencias de los modelos.** Stable Diffusion 1.5: CreativeML OpenRAIL-M; SDXL: OpenRAIL++-M; los de Ollama, cada una la suya. Léelas antes de un uso comercial.

## Contribuir
1. Haz un fork y una rama (`git checkout -b mejora/lo-que-sea`).
2. Instala los hooks: `pip install pre-commit detect-secrets && pre-commit install`.
3. Cambios pequeños y comentados. Si cambias la extracción o los prompts del resumidor, sube `EXTRACT_VERSION` / `PIPELINE_VERSION` en `src/pdf/summarizer.py` (si no, devuelve caché vieja).
4. Ejecuta las pruebas y, si tocas seguridad, `bandit` y `pip-audit`.
