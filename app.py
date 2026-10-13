"""Punto de entrada de Resumidor PDF Local. Lanza la interfaz gráfica."""
import sys
from pathlib import Path

# Asegura que el proyecto esté en el path sin importar desde dónde se ejecute
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.core.utils import ensure_package  # noqa: E402

# Si falta alguna dependencia, se instala automáticamente
ensure_package("customtkinter", "customtkinter")
ensure_package("markdown_it", "markdown-it-py")
ensure_package("tkinterdnd2", "tkinterdnd2", required=False)  # opcional: arrastrar y soltar
ensure_package("tkinterweb", "tkinterweb", required=False)    # opcional: visor HTML con fórmulas
ensure_package("matplotlib", "matplotlib", required=False)    # opcional: fórmulas como imágenes
ensure_package("pynvml", "nvidia-ml-py", required=False)      # opcional: detección de VRAM

from src.ui.app import run  # noqa: E402

if __name__ == "__main__":
    run()
