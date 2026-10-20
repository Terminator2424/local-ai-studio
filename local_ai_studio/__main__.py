"""Línea de comandos de Local AI Studio:  python -m local_ai_studio [--version | --doctor]

Sin argumentos abre la interfaz gráfica. La aplicación NUNCA instala paquetes por su cuenta: si falta alguno, lo dice
y muestra el comando exacto para instalarlo (instalar software en silencio sería una vía de ataque a la cadena de
suministro, justo lo que una aplicación cerrada y local debe evitar).
"""
from __future__ import annotations

import argparse
import importlib.util
import sys

from . import __version__

# módulo que se importa -> paquete de pip, y si es imprescindible
DEPENDENCIES = {
    "customtkinter": ("customtkinter", True),
    "markdown_it": ("markdown-it-py", True),
    "ollama": ("ollama", True),
    "pymupdf": ("pymupdf", True),
    "yaml": ("pyyaml", True),
    "PIL": ("pillow", True),
    "numpy": ("numpy", True),
    "sympy": ("sympy", True),
    "tkinterdnd2": ("tkinterdnd2", False),      # arrastrar y soltar
    "tkinterweb": ("tkinterweb", False),        # visor HTML con fórmulas
    "matplotlib": ("matplotlib", False),        # fórmulas como imágenes
    "pynvml": ("nvidia-ml-py", False),          # detección de VRAM
}


def missing_dependencies() -> tuple[list[str], list[str]]:
    """(imprescindibles que faltan, opcionales que faltan), como nombres de paquete de pip."""
    required: list[str] = []
    optional: list[str] = []
    for module, (package, needed) in DEPENDENCIES.items():
        if importlib.util.find_spec(module) is None:
            (required if needed else optional).append(package)
    return required, optional


def doctor() -> int:
    """Diagnóstico por consola: dependencias, hardware, Ollama y modelos."""
    from .core.config import load_config
    from .core.hardware_detector import describe
    from .firstrun import check_setup, essentials_missing, format_steps

    print(f"Local AI Studio {__version__} · Python {sys.version.split()[0]}\n")
    required, optional = missing_dependencies()
    print("Dependencias imprescindibles:", "todas instaladas" if not required else "FALTAN " + ", ".join(required))
    print("Dependencias opcionales:", "todas instaladas" if not optional else "faltan " + ", ".join(optional))
    cfg = load_config()
    print("\n" + describe(cfg))
    torch = None
    if importlib.util.find_spec("torch") is not None:
        try:
            import torch as _torch
            torch = {"version": _torch.__version__, "cuda": _torch.cuda.is_available()}
        except Exception as exc:
            torch = {"error": str(exc)}
    steps = check_setup(cfg, torch=torch)
    print("\nPrimeros pasos:\n" + format_steps(steps))
    return 1 if (required or essentials_missing(steps)) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local_ai_studio", description="Asistente de IA 100 % local.")
    parser.add_argument("--version", action="store_true", help="muestra la versión y termina")
    parser.add_argument("--doctor", action="store_true", help="diagnóstico: dependencias, GPU, Ollama y modelos")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):         # la consola de Windows (cp1252) no sabe escribir «✔» ni «→»
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.version:
        print(__version__)
        return 0
    if args.doctor:
        return doctor()
    required, _ = missing_dependencies()
    if required:
        print("Faltan dependencias imprescindibles: " + ", ".join(required), file=sys.stderr)
        print("Instálalas con:  python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    from .ui.app import run
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
