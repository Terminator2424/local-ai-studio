# Auditoría técnica de Local AI Studio

Revisión del repositorio como la haría un ingeniero de sistemas que evalúa un proyecto: estructura, calidad estática, pruebas,
seguridad, robustez, rendimiento, documentación y profesionalidad del repositorio. **Se midió primero, se corrigió después y se
volvió a medir.** El estado de partida es el commit `4f382f2`; el final, el de la rama actual.

- Fecha: 12-oct-2026 · Alcance: `local_ai_studio/` (≈ 10 900 líneas), `tests/` (≈ 3 400), scripts, configuración y documentación.
- Cómo repetirlo: ver [«Reproducir la auditoría»](#reproducir-la-auditoría).

## Calificación

| Dimensión | Antes | Después | Qué cambió |
|---|:-:|:-:|---|
| Estructura y arquitectura | 6 | **9** | El paquete se llamaba `src`; `utils.py` mezclaba 7 responsabilidades; había un ciclo `core ↔ pdf`; 3 copias de `sha256_file`. Ahora: paquete `local_ai_studio`, 15 módulos de una sola responsabilidad en `core/`, capas sin ciclos y vigiladas por una prueba. |
| Calidad estática | 5 | **9,5** | `ruff` 194 hallazgos reales → **0**; `mypy` más de 30 avisos → **0**; 7 funciones muertas, 1 opción de configuración muerta y 4 dependencias sin uso eliminadas; complejidad máxima F(58) → E(36), media A (4,4). |
| Pruebas | 6,5 | **8,5** | 232 → **352** pruebas. El orquestador `summarizer.py` (602 líneas) pasó de 0 % a 77 %; la **capa de lógica** está al **81 %** (global 53 %: la interfaz la ejercita un test de humo en un proceso aparte que `coverage` no mide). Nuevas: arquitectura, higiene del repo, interfaz real, almacenamiento, roles. |
| Seguridad | 8 | **9** | Se cerraron 3 huecos (instalación automática de paquetes, bomba de descompresión, una prueba de seguridad vacía). `bandit`: 0; `pip-audit`: sin vulnerabilidades conocidas. |
| Robustez y manejo de errores | 6,5 | **9** | Fugas de archivos al cancelar, índice no transaccional, caída con barra invertida final, cancelación accidental, configuración incompleta, reintentos absurdos. Todo corregido con prueba. |
| Rendimiento | 6 | **9** | «Documentos» abría en **10,5 s** y ahora en **0,4 s** (causa: IPv6 + un cliente HTTP por consulta). |
| Documentación | 7 | **9,5** | Se añadieron CAPACIDADES, ARQUITECTURA, esta auditoría, CONTRIBUTING y CHANGELOG; README y SECURITY reescritos con datos reales. |
| Profesionalidad del repositorio | 4 | **9** | Antes: sin LICENSE, sin CI, sin `pyproject.toml`, `.bat` con rutas absolutas, finales de línea sin fijar. Ahora: MIT, GitHub Actions (probado en un entorno limpio), `pyproject.toml` con ruff/mypy, dependabot, plantillas, `.gitattributes`. |
| Interfaz | 7 | **8,5** | Cabecera del chat que recortaba botones, selector de modelo con descripción, roles, filtro por documento, panel de almacenamiento y de red. Sin controles cortados en claro ni oscuro. |
| **Global** | **6,3** | **9,0** | |

> Las notas son la valoración del auditor, no una métrica automática; lo medible (ruff, mypy, pruebas, cobertura, complejidad,
> tiempos) está en las tablas y se puede repetir.
>
> Lo que impide el 10: la interfaz no tiene pruebas unitarias propias (solo humo), seis funciones siguen en complejidad D–E y el
> proyecto es solo para Windows. Están descritos abajo como deuda conocida, no ocultos.

## Hallazgos y correcciones

Gravedad: **Alta** = puede dar un resultado incorrecto o abrir una vía de ataque sin que nadie lo note; **Media** = fallo visible o
fragilidad real; **Baja** = mantenimiento. Cada corrección tiene una prueba que falla sin ella.

### Alta
| # | Hallazgo | Corrección | Prueba |
|---|---|---|---|
| A1 | `layout.py`: una regex tenía **caracteres backspace (0x08)** donde debía haber `\b` (la secuencia `\b` se guardó como carácter de control). No da error: la detección de código del PDF quedaba rota **en silencio**. | Restaurado `\b`. | `test_repo.test_ningun_archivo_tiene_caracteres_de_control` revisa todo el código |
| A2 | `test_security.py`: la **misma corrupción** convertía una aserción de seguridad del visor en una prueba que **nunca podía fallar**. | Restaurada; al activarla pasa (el visor sí es seguro). | la misma prueba de higiene |
| A3 | `app.py` ejecutaba `pip install` de paquetes al arrancar (`ensure_package`): una **vía de cadena de suministro** en una aplicación que presume de cerrada. | Eliminado. Ahora avisa de lo que falta y muestra el comando exacto. | `test_audit.test_si_falta_una_dependencia…` y `test_repo.test_la_aplicacion_nunca_instala…` |
| A4 | `images/edit.load` comprobaba el tope de 120 megapíxeles **después** de decodificar toda la imagen: una bomba de descompresión agotaba la memoria antes de rechazarse. | El tamaño se lee de la cabecera **antes** de decodificar. | `test_images.test_cargar_rechaza_una_bomba_de_descompresion_sin_decodificarla` |
| A5 | `latex_to_text` lanzaba `AttributeError` con una fórmula que **termina en `\`** (salida del modelo cortada), rompiendo el texto de respaldo del visor y la exportación. | Una barra final se conserva como carácter. | `test_audit.TestFormulasTruncadas` |
| A6 | `rag/index.py`: si indexar un PDF fallaba a mitad quedaba un **documento parcial** que un `commit` posterior confirmaba; las lecturas no usaban el cerrojo mientras otro hilo indexaba. | Inserción en una sola transacción; lecturas con cerrojo. | `test_audit.test_un_fallo_a_mitad_no_deja_un_documento_a_medias` |

### Media
| # | Hallazgo | Corrección |
|---|---|---|
| M1 | El extractor **no cerraba el PDF ni pdfplumber** al cancelar o fallar (en Windows el archivo queda bloqueado). | `try/finally` + `contextlib.closing`; prueba que borra el archivo tras cancelar. |
| M2 | **«Documentos» tardaba 5–10 s** en abrirse: `localhost` hace que Windows pruebe IPv6 primero (+2 s por conexión), se creaba un cliente HTTP por consulta (+0,3 s) y se consultaba a Ollama dos veces en el hilo de la interfaz. | `127.0.0.1` directo, cliente compartido, lista de modelos con caché de 10 s y consulta fuera del hilo de Tk: **10,5 s → 0,36 s**. |
| M3 | **Enter o clic durante una respuesta la cancelaba** (el envío se trataba como «Detener»); en «Documentos», Enter durante el indexado lo cancelaba. | Solo «Detener» o `Esc` interrumpen. |
| M4 | El registro en archivo solo se configuraba al abrir la página de PDF y no era idempotente (el panel de registro podía no engancharse). | `setup_logging()` al arrancar, idempotente. |
| M5 | `config.yaml` incompleto → `KeyError`; vacío → `TypeError`. | Se completa con `config.example.yaml`; un archivo roto no impide arrancar. |
| M6 | La cabecera del chat **recortaba «Exportar» y ocultaba «Herramientas»** con anchos reales (Windows escala al 125 %). | Cabecera rediseñada; acciones secundarias abajo. |
| M7 | La caché de resúmenes y las páginas renderizadas **crecían sin límite**. | Panel de almacenamiento, «Limpiar» seguro y poda automática (1 GB por carpeta, lo más antiguo primero). |
| M8 | **Ciclo `core ↔ pdf`** y `utils.py` de 300 líneas con 7 responsabilidades; 3 copias de `sha256_file`. | `latex_text` pasó a `core`; `utils` dividido; una sola implementación; prueba de capas. |
| M9 | Los reintentos a Ollama **dormían tras el último intento** y reintentaban errores permanentes (modelo inexistente). | No se reintenta un 4xx; no se duerme tras el último intento. |
| M10 | El filtro por documento se aplicaba **después** de quedarse con los 30 mejores: se perdían resultados del documento elegido. | Filtro antes del ranking (léxico y semántico). |
| M11 | Vectores de distinta dimensión (se cambió el modelo de embeddings) rompían la búsqueda con `ValueError`. | Se usa la dimensión mayoritaria. |
| M12 | `--doctor` fallaba al imprimir `✔`/`→` en la consola de Windows (cp1252). | Salida UTF-8. |

### Baja
`except: pass` silenciosos sustituidos por registro; funciones y una opción de configuración muertas eliminadas; `pypdf`, `tqdm`,
`rich` y `torchvision` sin uso fuera de `requirements`; `.replace(x, x)` sin efecto; variable de bucle sin enlazar en 2 lambdas;
una etiqueta ambigua («Modelo» → «Modelo para resumir PDF»); `.bat` sin `activate.bat` (ruta absoluta); finales de línea fijados.

## Lo que se comprobó que **no** cambió

Los refactores grandes (extractor, geometría, orquestador) se validaron comparando con el commit anterior:

- **Extracción** de «SIMPSOn (5) (2).pdf»: 16 páginas, 113 fórmulas por geometría → **idéntica byte a byte**.
- **Limpieza** del Markdown (19 683 caracteres, 17 encabezados) → **idéntica**.
- **Geometría tras refactorizar `mathgeom`** → idéntica (113 fórmulas).
- Respuesta real de «Documentos» con llama 3.1: «7», citando `metodos.pdf · p. 2` y **ignorando el documento desmarcado**.

## Deuda técnica conocida (no ocultada)

| Tema | Estado | Por qué se deja así |
|---|---|---|
| Interfaz sin pruebas unitarias propias | Cubierta por un test de humo que abre las 7 páginas y verifica comportamientos clave (modelos, roles, protocolo de la GPU, primer arranque). El uso real combinado (resumen + imagen + chat a la vez) se probó con modelos reales | Probar Tk a fondo exige un arnés propio; el coste supera el beneficio ahora. |
| Funciones de complejidad D–E: `_describe_figures`, `_page_with_geo`, `md_to_docx`, `process`, `add_pdf`, `drop_foreign_formulas`, `_convert`, `_fraction_items`, `_block_to_eq`, `_legacy_page_text` (`decide`, `build_chunks` y `_synthesize` ya se dividieron: resultado idéntico en 7 800 decisiones, 2 630 fragmentos y 144 síntesis) | Probadas (74–99 %), deterministas salvo las de LLM | Son algoritmos de una sola idea (plegado de fórmulas, mapa-reducción); dividirlas más oscurecería el flujo. |
| Cobertura baja en `mathocr` (26 %), `vision` (38 %), `clipboard` (0 %) | Opcionales o muy finas sobre APIs del sistema | `mathocr` es una tercera lectura desactivada; `clipboard` es ctypes de Windows. |
| Solo Windows | Declarado | Tk, NVML, Job Objects y portapapeles. La lógica sí es portable. |
| Solo Python 3.12 probado | Declarado en `pyproject.toml` | No hay 3.11 ni 3.13 en el entorno de pruebas. |
| `allow_network` es global al proceso | Documentado en SECURITY.md | Mientras dura una descarga confirmada, otro hilo también podría salir; las descargas son cortas y se piden explícitamente. |
| El sandbox no es una frontera absoluta | Documentado en SECURITY.md | Solo ejecuta código que ves y apruebas. |

## Reproducir la auditoría

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .                                              # estilo y errores: 0
python -m mypy local_ai_studio                                      # tipos: 0
python -m coverage run --source=local_ai_studio -m unittest discover -s tests
python -m coverage report --skip-empty                              # 352 pruebas
python -m bandit -r local_ai_studio app.py -ll                      # seguridad estática: 0
python -m pip_audit -r requirements.txt                             # dependencias: sin vulnerabilidades conocidas
python -m radon cc local_ai_studio -n C -s                          # complejidad
python -m local_ai_studio --doctor                                  # estado del equipo
```

La integración continua (`.github/workflows/ci.yml`) ejecuta lo mismo en cada cambio; se probó antes de publicar en un entorno de
Python limpio, sin PyTorch ni nada del entorno de desarrollo: ruff y mypy sin errores, 322 pruebas OK, bandit sin hallazgos y
pip-audit sin vulnerabilidades.
