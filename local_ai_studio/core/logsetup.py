"""Registro de la aplicación: archivo rotativo en logs/app.log y, opcionalmente, un manejador de la interfaz."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .paths import LOG_PATH


def setup_logging(extra_handler: logging.Handler | None = None) -> None:
    """Configura el logger raíz de la aplicación (`local_ai_studio`); es idempotente."""
    root = logging.getLogger("local_ai_studio")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if extra_handler:
        extra_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        root.addHandler(extra_handler)
