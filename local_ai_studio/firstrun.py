"""Asistente de primer arranque: qué falta para que la aplicación funcione y cómo arreglarlo.

Lo usan la ventana (aviso al abrir y tarjeta «Primeros pasos» en Ajustes) y `python -m local_ai_studio --doctor`. No instala nada:
solo comprueba y dice el comando exacto.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass

from .chat.engine import CHAT_MODELS, DEFAULT_MODEL, is_vision
from .core.ollama_client import get_client

VISION_MODEL = "qwen2.5vl:7b"
EMBED_MODEL = "nomic-embed-text"


@dataclass(frozen=True)
class Step:
    title: str
    ok: bool
    detail: str          # qué se encontró
    fix: str = ""        # qué hacer si no está bien
    essential: bool = False


def _has(installed: set[str], name: str) -> bool:
    return name in installed or f"{name}:latest" in installed


def check_setup(cfg: dict, torch: dict | None = None, installed: set[str] | None = None) -> list[Step]:
    """Los pasos necesarios, en orden. `installed` (modelos de Ollama) se consulta si no se da; `torch` es el informe de
    PyTorch (versión y CUDA) cuando ya se conoce; si no, solo se mira si está instalado (sin importarlo, que tarda)."""
    steps: list[Step] = []
    if installed is None:
        try:
            installed = {m.model for m in get_client(cfg).list().models}
        except Exception:  # noqa: BLE001  (cualquier fallo de conexión significa «Ollama no responde»)
            installed = None
    if installed is None:
        steps.append(Step("Ollama", False, "no responde en 127.0.0.1:11434",
                          "Instala Ollama desde https://ollama.com y ábrelo (debe quedar en la bandeja del sistema).", True))
        installed = set()
    else:
        steps.append(Step("Ollama", True, f"responde · {len(installed)} modelo(s) instalado(s)", essential=True))

    chat_models = [m for m in CHAT_MODELS if not is_vision(m) and _has(installed, m)]
    steps.append(Step("Modelo de chat", bool(chat_models),
                      ", ".join(chat_models) if chat_models else "ninguno instalado",
                      f"En una terminal: ollama pull {DEFAULT_MODEL}", True))
    steps.append(Step("Modelo de visión (PDF con fórmulas o escaneados)", _has(installed, VISION_MODEL),
                      VISION_MODEL if _has(installed, VISION_MODEL) else "no instalado",
                      f"En una terminal: ollama pull {VISION_MODEL}"))
    steps.append(Step("Búsqueda por sentido (Documentos)", _has(installed, EMBED_MODEL),
                      EMBED_MODEL if _has(installed, EMBED_MODEL) else "no instalado: se usa solo búsqueda por palabras",
                      f"En una terminal: ollama pull {EMBED_MODEL}"))

    if torch is not None and "error" not in torch:
        ok = bool(torch.get("cuda"))
        steps.append(Step("PyTorch con CUDA (generar imágenes)", ok,
                          f"PyTorch {torch.get('version')} · " + ("CUDA activo" if ok else "SIN CUDA: iría por CPU"),
                          "Instala la build CUDA: python -m pip install torch --index-url https://download.pytorch.org/whl/cu128"))
    else:
        present = importlib.util.find_spec("torch") is not None
        steps.append(Step("PyTorch (generar imágenes)", present,
                          "instalado (Ajustes comprueba si tiene CUDA)" if present else "no instalado: no se podrán generar imágenes",
                          "Mira requirements-images.txt (PyTorch con CUDA y después diffusers)."))
    return steps


def essentials_missing(steps: list[Step]) -> list[Step]:
    return [s for s in steps if s.essential and not s.ok]


def format_steps(steps: list[Step]) -> str:
    lines = []
    for s in steps:
        lines.append(f"{'✔' if s.ok else '✖' if s.essential else '○'} {s.title}: {s.detail}")
        if not s.ok and s.fix:
            lines.append(f"    → {s.fix}")
    return "\n".join(lines)
