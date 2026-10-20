"""Gestor de VRAM: con 8 GB no caben a la vez un LLM de Ollama y un pipeline de difusión.

Este módulo es el único que decide qué hay cargado:
- antes de generar imágenes descarga los modelos de Ollama (keep_alive = 0);
- antes de usar el chat o la visión libera el pipeline de difusión (y la caché de CUDA).
La interfaz muestra `state` para que se vea qué está en memoria y por qué hay una espera al cambiar de función.
"""
from __future__ import annotations

import gc
import logging
import sys
import threading
from collections.abc import Callable

from .ollama_client import get_client

log = logging.getLogger(__name__)

FREE = "libre"


class ModelManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._lock = threading.RLock()
        self.state = FREE                       # «libre», «chat: qwen2.5:7b» o «imagen: sd15»
        self._image_unloader: Callable[[], None] | None = None
        self.listeners: list[Callable[[str], None]] = []
        self._job_lock = threading.Lock()
        self._job: str | None = None            # el trabajo pesado en curso (la GPU solo admite uno a la vez)

    # --------------------------------------------------------- trabajo en curso
    @property
    def current_job(self) -> str | None:
        return self._job

    def begin_job(self, name: str) -> str | None:
        """Reserva la GPU para un trabajo pesado. Devuelve None si la consigue, o el nombre del trabajo que ya la tiene.
        Con 8 GB de VRAM un resumen y una imagen a la vez se echarían los modelos el uno al otro."""
        with self._job_lock:
            if self._job is not None:
                return self._job
            self._job = name
            return None

    def end_job(self, name: str) -> None:
        """Libera la reserva (solo si es la de `name`: un error nunca libera el trabajo de otro)."""
        with self._job_lock:
            if self._job == name:
                self._job = None

    # ------------------------------------------------------------------ estado
    def _set(self, state: str):
        self.state = state
        for fn in list(self.listeners):
            try:
                fn(state)
            except Exception:
                log.exception("Error en un oyente del gestor de modelos")

    def register_image_unloader(self, fn: Callable[[], None]):
        """El módulo de imágenes registra aquí cómo descargar su pipeline."""
        self._image_unloader = fn

    # ----------------------------------------------------------------- Ollama
    def running_llms(self) -> list[str]:
        try:
            return [m.model for m in get_client(self.cfg).ps().models]
        except Exception:
            return []

    def unload_llms(self) -> list[str]:
        """Pide a Ollama que descargue de la memoria todos los modelos cargados. Devuelve los que descargó."""
        names = self.running_llms()
        client = get_client(self.cfg)
        for name in names:
            try:
                client.generate(model=name, prompt="", keep_alive=0)
                log.info("Ollama: %s descargado de la VRAM", name)
            except Exception as exc:
                log.warning("No se pudo descargar %s: %s", name, exc)
        return names

    # ---------------------------------------------------------------- difusión
    def release_images(self) -> bool:
        """Libera el pipeline de difusión y la caché de CUDA. Devuelve True si había algo que liberar."""
        had = self.state.startswith("imagen")
        if self._image_unloader is not None:
            try:
                self._image_unloader()
            except Exception:
                log.exception("Error liberando el pipeline de imágenes")
        empty_cuda_cache()
        return had

    # ------------------------------------------------------------ transiciones
    def use_llm(self, model: str):
        """Llamar antes de chatear o de usar la visión."""
        with self._lock:
            if self.state.startswith("imagen"):
                self._set("liberando la VRAM de imágenes…")
                self.release_images()
            self._set(f"chat: {model}")

    def use_images(self, name: str):
        """Llamar antes de cargar o usar el pipeline de difusión."""
        with self._lock:
            if self.running_llms():
                self._set("descargando el modelo de chat de la VRAM…")
                self.unload_llms()
            self._set(f"imagen: {name}")

    def free_all(self):
        with self._lock:
            self.unload_llms()
            self.release_images()
            self._set(FREE)


def empty_cuda_cache():
    """torch.cuda.empty_cache() si torch ya está cargado y tiene CUDA. Si nadie importó torch no hay nada que vaciar
    (y así el chat no paga los segundos de importarlo)."""
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is None:
        return
    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception as exc:
        log.debug("No se pudo vaciar la caché de CUDA: %s", exc)


_MANAGER: ModelManager | None = None


def get_manager(cfg: dict | None = None) -> ModelManager:
    global _MANAGER
    if _MANAGER is None:
        if cfg is None:
            raise RuntimeError("Primer uso del gestor de modelos: hace falta la configuración.")
        _MANAGER = ModelManager(cfg)
    return _MANAGER
