"""Registro de la aplicación: archivo rotativo en logs/app.log y, opcionalmente, un manejador de la interfaz."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .paths import LOG_PATH


def setup_logging(extra_handler: logging.Handler | None = None) -> None:
    """Configura el logger raíz de la aplicación (`local_ai_studio`). Es idempotente: el archivo se configura una sola
    vez y cada manejador extra (el panel de registro de la interfaz) se añade una sola vez."""
    root = logging.getLogger("local_ai_studio")
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.setLevel(logging.INFO)
        fh = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        root.addHandler(fh)
    if extra_handler is not None and extra_handler not in root.handlers:
        extra_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        root.addHandler(extra_handler)
