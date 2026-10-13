"""Generación de imágenes con Stable Diffusion (diffusers), 100 % local.

- SD 1.5 en fp16 cabe entero en la GPU; SDXL va con `enable_model_cpu_offload` y VAE en mosaico para 8 GB.
- Antes de cargar el pipeline se descargan los modelos de Ollama (model_manager) y al descargarlo se libera la VRAM.
- torch/diffusers se importan solo al generar: el resto de la app no paga su tiempo de carga.
- Las imágenes se guardan en outputs/ (fuera de git) con el prompt y la semilla dentro del PNG.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..core.model_manager import ModelManager, empty_cuda_cache
from ..core.utils import Cancelled
from .models import CATALOG, OUTPUTS_DIR, ImageModel, is_installed

log = logging.getLogger("resumidor")

STEPS_RANGE = (1, 100)
CFG_RANGE = (1.0, 20.0)
SIDE_RANGE = (256, 1024)
BATCH_RANGE = (1, 4)


class GenerationError(RuntimeError):
    """Error explicable al usuario (sin memoria, modelo no instalado, parámetros fuera de rango…)."""


@dataclass
class GenParams:
    prompt: str
    negative: str = ""
    steps: int = 25
    cfg: float = 7.0
    width: int = 512
    height: int = 512
    seed: int | None = None          # None = aleatoria
    batch: int = 1

    def validate(self, model: ImageModel) -> "GenParams":
        if not self.prompt.strip():
            raise GenerationError("Escribe un prompt.")
        if not STEPS_RANGE[0] <= self.steps <= STEPS_RANGE[1]:
            raise GenerationError(f"Los pasos deben estar entre {STEPS_RANGE[0]} y {STEPS_RANGE[1]}.")
        if not CFG_RANGE[0] <= self.cfg <= CFG_RANGE[1]:
            raise GenerationError(f"CFG debe estar entre {CFG_RANGE[0]:g} y {CFG_RANGE[1]:g}.")
        for name, v in (("ancho", self.width), ("alto", self.height)):
            if not SIDE_RANGE[0] <= v <= SIDE_RANGE[1] or v % 8:
                raise GenerationError(f"El {name} debe ser múltiplo de 8 entre {SIDE_RANGE[0]} y {SIDE_RANGE[1]}.")
        if not BATCH_RANGE[0] <= self.batch <= BATCH_RANGE[1]:
            raise GenerationError(f"El lote debe estar entre {BATCH_RANGE[0]} y {BATCH_RANGE[1]}.")
        if self.seed is not None and not 0 <= self.seed < 2 ** 32:
            raise GenerationError("La semilla debe estar entre 0 y 4294967295.")
        # Fuera de la resolución nativa el modelo repite o deforma: se limita para que no salga una imagen mala sin avisar
        if self.width * self.height > (model.native * 1.5) ** 2:
            raise GenerationError(
                f"{model.title} está entrenado a {model.native}×{model.native}; por encima de ~{int(model.native * 1.5)} px "
                "por lado las imágenes salen duplicadas. Baja la resolución.")
        return self

    def seeds(self) -> list[int]:
        base = self.seed if self.seed is not None else random.randrange(2 ** 32)
        return [(base + i) % 2 ** 32 for i in range(self.batch)]


@dataclass
class Result:
    image: "object"               # PIL.Image.Image
    seed: int
    path: Path


class ImageGenerator:
    def __init__(self, manager: ModelManager):
        self.manager = manager
        self.pipe = None
        self.key: str | None = None
        self.lock = threading.Lock()
        manager.register_image_unloader(self.unload)

    # ------------------------------------------------------------------ carga
    def load(self, key: str, progress: Callable[[str], None] | None = None):
        model = CATALOG[key]
        if not is_installed(model):
            raise GenerationError(f"{model.title} no está descargado. Descárgalo desde la pestaña de imágenes.")
        with self.lock:
            if self.pipe is not None and self.key == key:
                return
            self.unload()
            self.manager.use_images(key)
            if progress:
                progress(f"Cargando {model.title}…")
            try:
                import torch
                from diffusers import DPMSolverMultistepScheduler, StableDiffusionPipeline, StableDiffusionXLPipeline
            except ImportError as exc:
                raise GenerationError("Esta instalación no incluye PyTorch/diffusers (la versión empaquetada .exe no los trae). "
                                      "Usa run.bat con el entorno completo; ver README.") from exc
            if not torch.cuda.is_available():
                raise GenerationError("PyTorch no encuentra la GPU (CUDA). Instala la build CUDA de PyTorch (ver README).")
            cls = StableDiffusionXLPipeline if model.xl else StableDiffusionPipeline
            kw = dict(dtype=torch.float16, variant="fp16", use_safetensors=True, local_files_only=True)
            if not model.xl:
                kw.update(safety_checker=None, feature_extractor=None, requires_safety_checker=False)
            pipe = cls.from_pretrained(str(model.dir), **kw)
            pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config, use_karras_sigmas=True)
            if model.xl:
                pipe.enable_model_cpu_offload()      # 8 GB: los módulos van a la GPU de uno en uno
                pipe.vae.enable_tiling()
            else:
                pipe.to("cuda")
                pipe.vae.enable_slicing()
            pipe.set_progress_bar_config(disable=True)
            self.pipe, self.key = pipe, key

    def unload(self):
        """Libera el pipeline y la VRAM (lo llama el gestor antes de usar el chat)."""
        if self.pipe is not None:
            log.info("Imágenes: liberando %s de la VRAM", self.key)
        self.pipe = None
        self.key = None
        empty_cuda_cache()

    # -------------------------------------------------------------- generación
    def generate(self, key: str, params: GenParams, cancel: threading.Event | None = None,
                 on_progress: Callable[[float, str], None] | None = None) -> list[Result]:
        model = CATALOG[key]
        params.validate(model)
        self.load(key, lambda m: on_progress and on_progress(0.0, m))
        import torch
        seeds = params.seeds()
        results: list[Result] = []
        total_steps = params.steps * len(seeds)
        done_steps = 0
        for n, seed in enumerate(seeds):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            gen = torch.Generator("cuda").manual_seed(seed)

            def step_cb(pipe, i, t, kw, _n=n):
                nonlocal done_steps
                if cancel is not None and cancel.is_set():
                    pipe._interrupt = True
                if on_progress:
                    on_progress((_n * params.steps + i + 1) / total_steps,
                                f"Imagen {_n + 1} de {len(seeds)} · paso {i + 1}/{params.steps}")
                return kw
            try:
                with torch.inference_mode():
                    out = self.pipe(prompt=params.prompt, negative_prompt=params.negative or None,
                                    num_inference_steps=params.steps, guidance_scale=params.cfg,
                                    width=params.width, height=params.height, generator=gen,
                                    callback_on_step_end=step_cb)
            except torch.cuda.OutOfMemoryError as exc:
                self.unload()
                raise GenerationError("Sin memoria de GPU. Baja la resolución, usa SD 1.5 o cierra otros programas "
                                      "que usen la GPU.") from exc
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            img = out.images[0]
            results.append(Result(img, seed, save_png(img, params, seed, key)))
        return results


def save_png(img, params: GenParams, seed: int, model_key: str) -> Path:
    """Guarda en outputs/ con el prompt, la semilla y los ajustes dentro del PNG (para poder repetir la imagen)."""
    from PIL.PngImagePlugin import PngInfo
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    meta = PngInfo()
    meta.add_text("prompt", params.prompt)
    meta.add_text("negative", params.negative)
    meta.add_text("settings", f"model={model_key} steps={params.steps} cfg={params.cfg} seed={seed} "
                              f"size={params.width}x{params.height}")
    path = OUTPUTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{model_key}_{seed}.png"
    n = 1
    while path.exists():
        path = path.with_name(f"{path.stem}_{n}.png")
        n += 1
    img.save(path, pnginfo=meta)
    return path
