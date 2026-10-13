# Capacidades de Local AI Studio

Inventario completo de lo que hace la aplicación, sección por sección, con sus límites. Todo lo que aquí se afirma está
implementado y cubierto por pruebas, salvo donde se indique lo contrario. Para el diseño interno, mira
[ARQUITECTURA.md](ARQUITECTURA.md); para la seguridad, [../SECURITY.md](../SECURITY.md).

**Índice** · [Principios](#principios) · [Resumir PDF](#1-resumir-pdf) · [Documentos](#2-documentos-preguntar-a-tus-pdfs) ·
[Chat](#3-chat) · [Asistente con herramientas](#4-asistente-con-herramientas-acciones-en-el-pc) · [Estudiar](#5-estudiar) ·
[Generar imagen](#6-generar-imagen) · [Editar imagen](#7-editar-imagen) · [Ajustes](#8-ajustes-y-diagnóstico) ·
[Memoria de vídeo](#9-memoria-de-vídeo-un-modelo-a-la-vez) · [Seguridad](#10-seguridad-resumen) ·
[Uso y empaquetado](#11-línea-de-comandos-configuración-y-empaquetado) · [Lo que NO hace](#12-lo-que-no-hace)

---

## Principios

1. **Todo es local.** Los documentos, los chats y las imágenes no salen del equipo. Una barrera de red a nivel de proceso
   bloquea cualquier conexión que no sea a `localhost`; la única excepción son las descargas de modelos que tú confirmas.
2. **La exactitud manda sobre la velocidad.** Lo que se puede comprobar con código (cifras, fórmulas, citas, código fuente)
   se comprueba con código; lo que no, se marca con ⚠ en lugar de darse por bueno.
3. **Nada que cambie algo ocurre sin tu permiso.** El asistente de acciones solo propone; tú ves la acción exacta y
   decides. Por defecto, denegar.
4. **Sin sorpresas de instalación.** La aplicación nunca instala paquetes por su cuenta ni descarga nada sin pedírtelo.

Pensada para una GPU de 8 GB (RTX 4060) y 16 GB de RAM, pero se adapta al hardware que detecta (sección 9).

---

## 1. Resumir PDF

Convierte un PDF en **Markdown compacto y fiel** (texto, tablas, código y fórmulas) para que un modelo de lenguaje lo lea con
muchos menos tokens que el PDF original, o para estudiarlo tú.

### 1.1 Entradas
| Función | Detalle |
|---|---|
| Un PDF, varios o una carpeta | «Elegir PDF…», «Añadir carpeta…» (busca PDFs en subcarpetas) o **arrastrar y soltar** sobre la ventana. |
| Cola de trabajo | Cada PDF muestra su estado (pendiente, en curso, listo, caché, error, cancelado). La cola **se guarda en disco**: si cierras la aplicación, al abrirla continúa donde se quedó. |
| Límites configurables | 200 MB y 800 páginas por defecto (`limits` en `config.yaml`). Un PDF cifrado, corrupto o vacío se rechaza **antes** de gastar tiempo de IA, con un mensaje claro. |
| Idioma | Detecta el idioma del documento (español, inglés, francés, portugués, alemán, italiano) y escribe en ese idioma; también puedes forzarlo. |

### 1.2 Extracción del texto
- **Texto vectorial con PyMuPDF** en orden de lectura, con las **tablas reconstruidas** a partir de la posición de las palabras
  (celdas separadas por huecos) y respetando columnas.
- **Encabezados, pies y números de página** repetidos se eliminan, pero solo si no vacían el documento; un número suelto en
  medio del texto (numerador de una fracción, celda de tabla) se conserva.
- **Tildes sueltas de LaTeX** (`Integraci´on` → `Integración`) y palabras cortadas por guion al final de línea se corrigen.
- **Respaldo con pdfplumber** cuando una página casi no tiene texto.
- **OCR con visión** (`qwen2.5vl:7b`) para páginas escaneadas; si hay dos lecturas y difieren, las líneas dudosas salen con ⚠.
- El PDF se **cierra siempre**, también al cancelar o fallar (en Windows un archivo abierto no se puede mover ni borrar).

### 1.3 Fórmulas (el punto más delicado)
Un modelo de 7B se equivoca al reescribir matemáticas, así que en el nivel por defecto **ningún modelo reescribe el contenido**:

- **Reconstrucción desde la geometría del PDF** (`mathgeom`): un PDF generado con LaTeX guarda cada símbolo con su **fuente**
  (`CMMI10`, `CMR10`, `CMSY10`, `CMEX10`…), su **tamaño** y su **línea base**; las fracciones son líneas horizontales. Con eso se
  reconstruye el LaTeX **sin modelo**: fracciones, subíndices y superíndices, integrales y sumatorios con sus límites, y símbolos
  como ±, ≤, ≥, ≈, ≠, ∞, ∂, ∇, ×, · o °. Lo que no se puede explicar (una figura, un recuadro) se marca como incompleto y se deja a la visión.
- **Doble lectura de la visión** a 200 y 300 dpi. Si discrepan entre sí o con la geometría, la fórmula sale con ⚠ y dice **qué
  símbolo** difiere («PDF «3» · visión «2»»).
- **Tercera lectura opcional** con `pix2tex` (CPU, desactivada por defecto) para contrastar símbolos.
- Cada fórmula se **verifica**:
  - las **cifras** que contiene deben existir en el texto del PDF;
  - las **cadenas numéricas** (`a = b = c`) se recalculan con un evaluador propio sin `eval` y se comparan con la tolerancia de los
    decimales escritos;
  - las **identidades simbólicas** se comprueban con sympy sobre puntos de prueba (`(x+1)^2 = x^2+1` se marca ✗); si la expresión
    es ambigua o tiene símbolos distintos a cada lado, **se abstiene** en vez de opinar;
  - se avisa de **operadores de más** que la visión pudo inventar.

### 1.3b Código y tablas
Cada bloque de código detectado se comprueba: **cada línea debe existir en el PDF**, los delimitadores `()[]{}` deben cuadrar
(ignorando cadenas, comentarios y expresiones regulares) y, en Python, el código debe **compilar**. Las tablas se mantienen como
tablas Markdown.

### 1.4 Niveles
| Nivel | Qué hace | Cuándo usarlo |
|---|---|---|
| **Texto limpio** (por defecto) | Todo el contenido, ordenado y sin basura. **Cero reescritura por IA**: solo extrae, limpia y verifica. El único texto del modelo es una descripción de 3–5 frases al principio (con sus cifras auditadas). | Casi siempre: es el más fiel y ahorra ≈ 90 % de tokens frente a enviar el PDF. |
| **Compacto IA** | Telegráfico: lo que una IA necesita para entender el documento, con el mínimo de tokens. | Documentos largos de mucha prosa. |
| **Normal** | Equilibrado: más contexto y explicaciones breves. | Estudiar un documento. |
| **Detallado** | Lo más completo; puede ser tan largo como el PDF. | Cuando no se puede perder nada. |

> El motor tiene además «Ultra-corto» (ruta Map-Reduce clásica) en `config.yaml`; la interfaz muestra los cuatro de arriba.

### 1.5 Pipeline de los niveles con IA
1. **Troceo por secciones** (`Problema 1`, `Capítulo 2`…) con su rango de páginas.
2. **Nota exhaustiva por fragmento** (ideas, datos, fórmulas, enunciados, conclusiones, figuras).
3. **Verificación**: cada nota se audita contra el texto de *su* fragmento; solo se acepta la versión corregida si no empeora.
4. **Saneado determinista** sin IA: quita fórmulas ajenas al fragmento, cifras sin respaldo, numeración inventada, relleno y
   restituye tablas omitidas.
5. **Síntesis**: el cuerpo son las notas verificadas; el modelo solo redacta el resumen ejecutivo y las conclusiones, con
   *Self-Refine* (crítica + mejora).
6. **Auditoría numérica final**: porcentaje de cifras del documento cubiertas y lista de cifras del resumen **sin respaldo**.
7. **Descripción de gráficos** con visión (opcional, hasta 40 páginas con figuras por documento).

### 1.6 Salidas
- **Informe de fidelidad** al final del documento: «fórmulas verificadas 96/101 · ⚠ 2 a revisar (págs. 3, 9)», bloques de código
  comprobados y avisos por página.
- **Estadísticas de tokens**: tamaño del resumen frente al PDF (con y sin imágenes de página) y ahorro en %.
- **Guardar** crea `.md`, **`.pdf`** (A4, con las fórmulas dibujadas) y **`.docx`** (Word, con tablas, listas y fórmulas como imagen).
- **Copiar para IA** pone el Markdown en el portapapeles listo para pegarlo en cualquier modelo.
- **Visor** con Markdown y LaTeX renderizado; modo seguro (sin imágenes ni enlaces externos).

### 1.7 Caché y opciones
Cada resultado se guarda por **hash del PDF + ajustes + versión del pipeline**: volver a resumir el mismo PDF es instantáneo, y
cambiar un ajuste regenera. Opciones: leer fórmulas con visión, leerlas dos veces, verificar contra el PDF, describir gráficos,
usar caché, tamaño de fragmento, solape e iteraciones de refinado. Se pueden cancelar en cualquier momento (no queda nada a medias).

---

## 2. Documentos: preguntar a tus PDFs

Indexa uno o muchos PDFs y pregúntales en lenguaje natural; cada respuesta **cita la página**.

| Función | Detalle |
|---|---|
| Indexado | «Indexar PDFs…» o una carpeta (hasta 200 PDFs por carpeta, hasta 1500 páginas por PDF). El mismo archivo no se indexa dos veces (por SHA-256). Se guarda en `conversations/rag.db` (SQLite). |
| Búsqueda **híbrida** | **Léxica** (SQLite FTS5 con BM25: encuentra cifras, nombres y fórmulas exactas, que los embeddings pierden) + **semántica** (embeddings `nomic-embed-text` en Ollama). Se fusionan por rango (RRF). Sin el modelo de embeddings, funciona solo con la búsqueda léxica. |
| Elegir en qué documentos preguntar | Una **casilla por documento**: preguntas solo a los marcados. El filtro se aplica *antes* de ordenar los resultados, así que no se pierden respuestas del documento elegido. |
| Respuestas con citas | El modelo recibe fragmentos numerados `[1] p. 7…` y debe citarlos. La respuesta se muestra con las **fuentes** y una **cita literal** de cada fragmento usado. |
| Comprobación automática | Se verifica que las **citas existan** y que las **cifras de la respuesta aparezcan** en los fragmentos; lo contrario se avisa con ⚠. Si no hay nada relevante, responde «no lo encuentro» en vez de inventar. |
| Texto del documento = datos | Un PDF con «ignora lo anterior…» no puede dar órdenes: su texto se trata como contenido, no como instrucciones. |
| Selector de modelo | Con descripción de cada modelo (ver §3). Por defecto, Llama 3.1 8B. |

Detalles: fragmentos de ≈ 900 caracteres con solape de 120 y número de página; las consultas se limpian para que nadie pueda
inyectar operadores en FTS5; el índice se escribe en una sola transacción (o se guarda el documento entero o nada).

---

## 3. Chat

Conversación con modelos locales, con historial.

### 3.1 Modelos (selector con descripción)
| Modelo | Para qué | Tamaño | Herramientas |
|---|---|---|---|
| **Llama 3.1 8B** *(predeterminado)* | uso general; razona bien y sigue instrucciones | 4,9 GB | sí |
| Qwen 2.5 7B | muy bueno en español y datos/tablas | 4,7 GB | sí |
| Gemma 2 9B | redacción cuidada, más lento (se reparte con la CPU en 8 GB) | 5,4 GB | no |
| Mistral 7B | ligero y rápido | 4,4 GB | sí |
| Qwen 2.5 Coder 7B | programación | 4,7 GB | sí |
| Qwen 2.5 VL 7B | **visión**: lee imágenes (fotos, capturas, PDFs escaneados) | 6,0 GB | no |

El selector muestra «Nombre — para qué sirve» y, debajo, el detalle del elegido (y si falta instalarlo). Si eliges uno no instalado,
la app pide permiso y lo descarga (solo de la lista cerrada de la aplicación).

### 3.2 Roles
Un system prompt listo para cada trabajo, elegible en la cabecera (se puede editar el texto; entonces aparece «Personalizado»):
**Asistente general**, **Tutor de ingeniería** (paso a paso con ejemplo numérico y comprobación), **Programador** (código mínimo,
casos límite, cómo probarlo), **Seguridad (defensiva)** (amenazas, revisión de código, endurecimiento y CTF en entornos
autorizados; explica siempre la corrección) y **Redactor académico** (mejora la redacción sin inventar fuentes). Todos piden
decir «no lo sé» antes que inventar.

### 3.3 Conversación
- **Streaming** palabra a palabra, con **estadísticas** (tokens, tokens/s, segundos).
- **Historial en SQLite**: lista de conversaciones con título automático, abrir, borrar; cada conversación recuerda su modelo y su rol.
- **Parámetros**: temperatura, top_p, tokens máximos (16–32 768) y contexto (512–131 072). Se recuerdan entre sesiones.
- **Imágenes** (solo con el modelo de visión): adjuntar una o varias; la app cambia sola al modelo de visión si hace falta.
- **Regenerar** la última respuesta y **exportar la conversación a Markdown**.
- **Recorte automático del historial**: si no cabe en el contexto, se quitan los mensajes más antiguos (nunca el system ni el último).
- **Markdown y LaTeX** renderizados en modo seguro; con código, tablas y fórmulas.
- **Detener**: botón «Detener» o `Esc`. Pulsar Enter durante una respuesta no la cancela por accidente.
- **Teclado**: `Enter` envía, `Mayús+Enter` salto de línea.

---

## 4. Asistente con herramientas (acciones en el PC)

El interruptor **Herramientas** (modelos Llama 3.1, Qwen 2.5, Qwen Coder, Mistral) deja que el modelo pida acciones. **No hay una
terminal libre**: solo existen estas ocho herramientas, cada una con su política.

| Herramienta | Qué hace | Permiso |
|---|---|---|
| `listar_carpeta` | Lista una carpeta permitida (hasta 200 entradas) | automático |
| `leer_archivo` | Lee texto o PDF (hasta 200 KB; rechaza binarios) | automático |
| `buscar_archivos` | Busca por nombre/comodín (hasta 100 resultados, sin rutas) | automático |
| `calcular` | Calcula/simplifica una expresión LaTeX o simbólica con sympy (sin `eval`) | automático |
| `fecha_hora` | Fecha y hora del equipo | automático |
| `escribir_archivo` | Crea un archivo de texto en `workspace/` (no sobrescribe salvo petición; nunca `.exe`, scripts, accesos directos…) | **te lo pide cada vez** |
| `ejecutar_python` | Ejecuta código Python **aislado** | **te lo pide cada vez** |
| `abrir_archivo` | Abre un documento o imagen con su programa (nunca ejecutables) | **te lo pide cada vez** |

**La ventana de permiso** muestra la acción **completa** (todo el código, todo el contenido), con **Denegar** como opción por
defecto; `Esc`, cerrar o 3 minutos sin respuesta = denegar. Si la acción se pidió *después* de leer un archivo o PDF, avisa en rojo
de que ese contenido pudo traer instrucciones ocultas. Lo que sea demasiado largo para revisarlo (> 20 000 caracteres) se rechaza.

**Política de rutas**: por defecto solo `workspace/` (lectura y escritura). Puedes añadir carpetas de **solo lectura**. Se
resuelven `..`, uniones y enlaces; se rechazan rutas UNC, flujos alternativos de NTFS (`a.txt:oculto`), nombres reservados
(`CON`, `NUL`…) y secretos (`.ssh`, `.env`, `id_rsa`, `*.pem`, credenciales…) aunque estén dentro de una carpeta permitida. No se
aceptan unidades enteras, carpetas del sistema ni tu perfil completo.

**Sandbox del código**: intérprete de Python aislado en una carpeta temporal; gancho de auditoría propio (sin red, sin procesos,
sin `ctypes`, sin archivos fuera de su carpeta); **Job Object** de Windows (256 MB de memoria, un solo proceso, se mata al cerrar);
10 s de tiempo y 20 000 caracteres de salida como máximo. En la versión `.exe` no está disponible.

**Reglas fijas del asistente**: el contenido leído son *datos*, no órdenes; si lo rechazas no insiste; hasta 6 pasos de herramientas
por petición. Todo queda en `conversations/actions.log`.

---

## 5. Estudiar

Convierte un documento (o texto pegado) en material de estudio, **verificando con código** lo que se puede verificar.

| Tarea | Resultado |
|---|---|
| **Tarjetas de estudio** | 6–12 tarjetas pregunta/respuesta; **exportar a CSV para Anki** (se neutralizan las fórmulas de hoja de cálculo para que un CSV no ejecute nada al abrirlo). |
| **Preguntas de práctica** | 5–8 preguntas de dificultad creciente, con las respuestas razonadas al final. |
| **Resolver un ejercicio paso a paso** | Resolución desglosada; **cada igualdad se marca ✓ (verificada), ✗ (incorrecta) o ⚠ (no verificable)** con cálculo exacto, no con la opinión del modelo. |

- **Fuente**: un documento ya indexado (con rango de páginas) o texto pegado.
- **Código Python del resultado**: botón «Ejecutar bloque» que lo ejecuta en el sandbox **tras mostrarte el código completo y pedir
  permiso**, sin red y sin acceso a tus archivos.
- El texto del documento se trata como datos (resistente a instrucciones ocultas). Mismo selector de modelo con descripciones.

---

## 6. Generar imagen

Stable Diffusion **100 % local** en tu GPU.

| Función | Detalle |
|---|---|
| Modelos | **Stable Diffusion 1.5** fp16 (≈ 2,3 GB, 512×512 nativo, ≈ 24 s para 2 imágenes, ≈ 4,4 GB de VRAM) y **SDXL base 1.0** fp16 (≈ 6,9 GB, 1024×1024 nativo; con 8 GB usa descarga parcial a CPU: ≈ 53 s por imagen y ≈ 5,2 GB de pico). |
| Parámetros | Prompt y *negative prompt*, pasos (20–50), CFG (1–15), resolución (7 preajustes de 512 a 1024, siempre múltiplos de 8), semilla fija o aleatoria, **lote de 1 a 4**. Planificador DPM++ con sigmas de Karras. |
| Protección | La resolución se limita a 1,5× la nativa del modelo (más allí salen imágenes duplicadas); los parámetros fuera de rango se rechazan con un mensaje. |
| Resultado | Vista previa con miniaturas, **guardar como**, **copiar**, **abrir la carpeta** `outputs/` y **enviar al editor**. Cada PNG lleva dentro el prompt, la semilla y los ajustes (se puede repetir la imagen). |
| Descarga verificada | Solo se descargan archivos **`.safetensors`** (sin código ejecutable), de un **commit fijado** del repositorio, con el **SHA-256** de cada peso fijado en el código; si no coincide, se borra. El modelo se carga con `local_files_only=True`. |
| Cancelar | Interrumpe entre pasos y libera la VRAM. |

Requiere PyTorch con CUDA (ver README). Sin él, la pestaña lo explica en lugar de fallar.

---

## 7. Editar imagen

Editor sencillo con **vista previa en vivo** (Pillow).

- **Abrir** con diálogo, **arrastrando y soltando** o enviada desde «Generar imagen».
- **Giro** de 90° a izquierda/derecha y 180°, **volteo** horizontal y vertical.
- **Recorte** quitando un % de cada lado.
- **Tamaño final** en píxeles (hasta 16 384 por lado) con botón «Tamaño original».
- **Ajustes**: brillo, contraste, saturación, desenfoque, nitidez, sepia y escala de grises; «Restablecer todo».
- **Guardar** como PNG, JPEG o WebP, y **copiar al portapapeles** (formato nativo de Windows, para pegar en otras aplicaciones).
- **Tope de 120 megapíxeles** comprobado en la cabecera *antes* de decodificar (una imagen que declara miles de millones de
  píxeles se rechaza sin agotar la memoria).

---

## 8. Ajustes y diagnóstico

- **GPU y memoria**: modelo de GPU, VRAM total y libre, RAM.
- **Configuración aplicada**: lo que decidió el ajuste automático según tu hardware (ver §9) y el modelo que se usa para resumir PDF.
- **PyTorch**: versión y si tiene CUDA, con el mensaje exacto de qué hacer si no.
- **En memoria ahora**: qué modelo ocupa la VRAM y **«Liberar toda la VRAM»**.
- **Modelos de chat**: instalados o no, con su descripción, y **descarga con confirmación**.
- **Modelos de imágenes**: estado y **verificación de hashes** de lo descargado.
- **Almacenamiento**: cuánto ocupa cada carpeta (modelos, imágenes generadas, chats e índice, resúmenes, caché, páginas
  renderizadas, registros), «Abrir carpeta» y **«Limpiar»** para lo que se puede regenerar (caché y páginas). Tus resúmenes, chats,
  documentos e imágenes **nunca** se borran desde aquí. Además, al arrancar la aplicación **poda en segundo plano** lo más antiguo
  de esas dos carpetas si superan 1 GB cada una.
- **Copiar informe** del diagnóstico (no contiene tus documentos).
- **Modo oscuro**: interruptor en la barra lateral (se recuerda entre sesiones).

---

## 9. Memoria de vídeo: un modelo a la vez

Con 8 GB no caben a la vez un modelo de chat y uno de imágenes. El **gestor de VRAM** decide qué está cargado:

- Antes de generar imágenes **descarga los modelos de Ollama** (`keep_alive=0`); antes de chatear **libera el pipeline de imágenes**
  y vacía la caché de CUDA.
- **Un solo trabajo pesado a la vez.** Resumir PDF, Generar imagen, Chat, Documentos (indexar o preguntar) y Estudiar reservan la GPU
  al empezar; si otro ya la tiene, el segundo **se niega con un aviso** («Ahora mismo está en curso: …») y no pierde lo escrito.
  «Liberar toda la VRAM» también se bloquea mientras haya un trabajo en curso. Sin esto, dos trabajos a la vez se echarían los modelos
  el uno al otro y uno acabaría con un error de memoria.
- La **barra de estado** muestra qué hay en memoria, la VRAM libre y el trabajo en curso («Trabajando: …»); al cambiar de función hay una espera de unos segundos.
- **Ajuste automático al hardware** (sin tocar tu `config.yaml`; se desactiva con `hardware.auto: false`):

| VRAM | Modelo de resumen | Contexto |
|---|---|---|
| ≥ 12 GB | `qwen2.5:14b` si está instalado | 8 192–16 384 |
| 8–12 GB | `qwen2.5:7b` | 16 384 |
| 6–8 GB | `qwen2.5:7b` | 8 192 |
| 4–6 GB | `qwen2.5:7b` | 4 096 |
| < 4 GB o sin GPU NVIDIA | `qwen2.5:7b` (o `3b` con < 8 GB de RAM) en CPU | 2 048–4 096 |

Si Ollama ya tiene un modelo cargado, se descuenta esa VRAM del cálculo; nunca se sube el contexto por encima del de tu config.

---

## 10. Seguridad (resumen)

Detalle, modelo de amenazas, pruebas y **límites honestos** en [../SECURITY.md](../SECURITY.md). En una línea por capa:

- **Red**: gancho de auditoría (PEP 578) que aborta cualquier conexión o DNS a un host que no sea de bucle local; Ollama solo en
  `127.0.0.1` (se niega a hablar con otro host).
- **Visor**: HTML escapado, sin imágenes ni enlaces externos.
- **Acciones**: lista cerrada de herramientas, permiso con la acción completa, política de rutas, sandbox, registro de auditoría.
- **Descargas**: listas cerradas, solo `.safetensors`, commit y SHA-256 fijados.
- **Dependencias**: la aplicación nunca instala paquetes; las versiones están acotadas y auditadas (`pip-audit`, `bandit`).
- **Datos**: `.gitignore` excluye modelos, chats, salidas y configuración; `detect-secrets` en cada commit.

---

## 11. Línea de comandos, configuración y empaquetado

```bash
python -m local_ai_studio            # abre la interfaz (o run.bat con doble clic)
python -m local_ai_studio --doctor   # diagnóstico: dependencias, GPU, Ollama y modelos
python -m local_ai_studio --version
```

- **Configuración**: `config.yaml` (se crea desde `config.example.yaml` la primera vez). Lo que falte se completa con los valores
  por defecto; un archivo roto no impide arrancar; un `host` de Ollama que no sea local se rechaza.
- **Datos portátiles / aislados**: la variable de entorno `LOCAL_AI_STUDIO_HOME` mueve **todos** los datos (config, chats, modelos,
  resúmenes, registros) a otra carpeta. Las pruebas la usan para no tocar tus datos.
- **Registro**: `logs/app.log` (rotativo, 2 MB × 4) y un panel de registro en la pestaña de PDF.
- **Versión empaquetada (.exe)**: `build_exe.bat` genera `dist\LocalAIStudio\` con PyInstaller (≈ 270 MB). Sirve para PDF,
  documentos, chat, estudiar y editar imágenes. **No incluye PyTorch** (no genera imágenes) **ni el sandbox** de código. Sigue
  necesitando Ollama.
- **Pruebas y calidad**: `python -m unittest discover -s tests` (más de 300 pruebas, sin Ollama ni GPU), `ruff`, `mypy`, `bandit`,
  `pip-audit`, integración continua en GitHub Actions.

---

## 12. Lo que NO hace

- **No controla todo el PC.** Solo las ocho herramientas de §4; no hay terminal libre, red, borrado, instalación de software, registro
  de Windows ni acceso a claves o carpetas del sistema. Es una decisión de diseño, no una carencia.
- **No quita las salvaguardas de los modelos.** Las restricciones están en los pesos de cada modelo (Llama, Qwen, Gemma, Mistral); la
  aplicación no las modifica ni ofrece versiones «sin censura». Para trabajo de seguridad legítimo existe el rol «Seguridad (defensiva)».
- **No entrena modelos.** Para usar *tus* documentos no hace falta: lo cubre «Documentos». Ajustar un modelo (LoRA) solo serviría para
  cambiar estilo o formato.
- **No garantiza la verdad de lo que dice un modelo.** Reduce los errores comprobables (cifras, fórmulas, citas, código) y marca lo
  que no puede comprobar; un documento crítico necesita revisión humana.
- **No es multiplataforma**: usa Windows (Tk, NVML, Job Objects, portapapeles). La lógica (PDF, índice, verificación) es portable.
- **No sube nada**: ni telemetría, ni cuentas, ni sincronización.
