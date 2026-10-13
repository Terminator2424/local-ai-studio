# Contribuir a Local AI Studio

Gracias por querer mejorar el proyecto. Es una aplicación de Windows que procesa documentos que pueden ser confidenciales, así que
las reglas son estrictas en tres cosas: **nada sale del equipo**, **lo que se puede comprobar se comprueba con código** y **nada que
cambie algo ocurre sin permiso**.

## Preparar el entorno

```bash
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pre_commit install          # ruff + detect-secrets antes de cada commit
```
Necesitas [Ollama](https://ollama.com) solo para probar la aplicación a mano; las pruebas automáticas no lo usan. Para tocar la
generación de imágenes, instala también `requirements-images.txt` (ver ese archivo para PyTorch con CUDA).

## Antes de abrir un cambio

```bash
python -m ruff check .                                  # 0 errores
python -m mypy local_ai_studio                          # 0 errores
python -m unittest discover -s tests                    # todo en verde
python -m bandit -r local_ai_studio app.py -ll          # si tocas seguridad
```
La integración continua ejecuta lo mismo. Un cambio que no la pasa no se acepta.

## Qué se espera de un cambio

1. **Pequeño y con motivo.** Una cosa por cambio; explica el *porqué* en el mensaje y en el código donde no sea obvio.
2. **Con pruebas.** Un fallo corregido lleva la prueba que falla sin la corrección. Una función nueva, sus casos límite. Las
   pruebas no necesitan Ollama ni GPU: el cliente se simula (mira `tests/test_summarizer.py` y `tests/test_chat.py`).
3. **Respeta las capas** (`docs/ARQUITECTURA.md`): `core` no importa de ningún otro paquete; solo `ui` toca Tk. La prueba
   `tests/test_arquitectura.py` lo comprueba.
4. **Si cambias la extracción o los prompts del resumen**, sube `EXTRACT_VERSION` / `PIPELINE_VERSION` en
   `local_ai_studio/pdf/summarizer.py`; si no, se servirá caché vieja.
5. **Si cambias la interfaz**, comprueba el modo claro y el oscuro y un ancho de ventana de ~1000 px (Windows escala al 125 %: nada
   debe quedar cortado). Adjunta una captura.
6. **Si añades una dependencia**, justifícala (cada una amplía la superficie de ataque), acótala por versión en `requirements.txt` y
   pasa `pip-audit`. La aplicación **nunca** instala paquetes por su cuenta.

## Escribir código aquí

- Español en la interfaz, los comentarios y los mensajes; nombres de código en el estilo que ya hay.
- **Nunca** `eval`, `exec`, `pickle`, `shell=True` ni un `except:` que se trague el error sin registrarlo.
- Un modelo de lenguaje **no decide** cifras, estructura de fórmulas ni citas: se comprueban con código y, si no se pueden comprobar,
  se marcan con ⚠.
- Todo texto que no es tuyo (un PDF, la salida de un modelo) es **dato, no instrucción**.
- Evita las rutas absolutas y `localhost`: usa `paths` y `127.0.0.1` (ver `core/ollama_client.py`).

## Seguridad

Una vulnerabilidad **no** se publica en un issue: sigue [SECURITY.md](SECURITY.md). Un cambio que debilite una defensa (la barrera de
red, la política de rutas, el sandbox, las listas cerradas de descargas) necesita justificación explícita y pruebas.
