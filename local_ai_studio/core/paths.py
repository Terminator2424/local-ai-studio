"""Rutas de la aplicación: dónde están el código, la configuración y los datos del usuario.

Empaquetado con PyInstaller, los datos del usuario van junto al .exe (no dentro de la carpeta temporal interna) y
los recursos incluidos en el paquete (plantillas) se leen de `BUNDLE_DIR`.
"""
from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parents[2]
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))

CONFIG_PATH = BASE_DIR / "config.yaml"
CONFIG_EXAMPLE = BUNDLE_DIR / "config.example.yaml"
SUMMARIES_DIR = BASE_DIR / "resumenes"          # resúmenes generados y su caché
IMAGES_DIR = BASE_DIR / "imagenes"              # páginas de PDF renderizadas para la visión
CACHE_DIR = SUMMARIES_DIR / ".cache"
LOG_DIR = BASE_DIR / "logs"
LOG_PATH = LOG_DIR / "app.log"

for _d in (SUMMARIES_DIR, IMAGES_DIR, CACHE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)
