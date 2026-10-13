"""Rutas de la aplicación: dónde están el código, la configuración y los datos del usuario.

- Normal: los datos van en la carpeta del proyecto.
- Empaquetado con PyInstaller: los datos van junto al .exe (no dentro de la carpeta temporal interna) y los recursos
  incluidos en el paquete (plantillas) se leen de `BUNDLE_DIR`.
- `LOCAL_AI_STUDIO_HOME`: carpeta alternativa para TODOS los datos (config, chats, modelos, resúmenes…). Sirve para
  instalaciones portátiles y para que las pruebas no toquen tus datos reales.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
_PROJECT = Path(__file__).resolve().parents[2]
_HOME = os.environ.get("LOCAL_AI_STUDIO_HOME", "").strip()

if FROZEN:
    BASE_DIR = Path(sys.executable).resolve().parent
elif _HOME:
    BASE_DIR = Path(_HOME).resolve()
else:
    BASE_DIR = _PROJECT
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", _PROJECT))

CONFIG_PATH = BASE_DIR / "config.yaml"
CONFIG_EXAMPLE = BUNDLE_DIR / "config.example.yaml"
SUMMARIES_DIR = BASE_DIR / "resumenes"          # resúmenes generados y su caché
IMAGES_DIR = BASE_DIR / "imagenes"              # páginas de PDF renderizadas para la visión
CACHE_DIR = SUMMARIES_DIR / ".cache"
LOG_DIR = BASE_DIR / "logs"
LOG_PATH = LOG_DIR / "app.log"

for _d in (SUMMARIES_DIR, IMAGES_DIR, CACHE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)
