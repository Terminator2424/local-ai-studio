"""Catálogo y descarga de modelos de imagen (Stable Diffusion).

Seguridad:
- Solo pesos `.safetensors` (formato sin código ejecutable; nunca `.ckpt` ni pickle de origen dudoso).
- La revisión del repositorio está fijada (commit exacto) y cada peso grande tiene su SHA-256 fijado aquí:
  si el archivo descargado no coincide, se borra y se avisa.
- Las descargas solo ocurren cuando el usuario lo confirma en la interfaz (download() no se llama sola).
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from ..core.netguard import allow_network
from ..core.paths import BASE_DIR
from ..core.textutil import sha256_file

log = logging.getLogger(__name__)

MODELS_DIR = BASE_DIR / "models"
OUTPUTS_DIR = BASE_DIR / "outputs"


@dataclass(frozen=True)
class ImageModel:
    key: str
    title: str
    repo: str
    revision: str                     # commit exacto del repositorio
    native: int                       # resolución nativa (lado en px)
    license: str
    files: dict = field(default_factory=dict)       # ruta -> sha256 (vacío = archivo pequeño sin hash fijado)
    xl: bool = False
    size_mb: int = 0                  # tamaño aproximado de la descarga
    note: str = ""

    @property
    def dir(self) -> Path:
        return MODELS_DIR / self.key


_SD15 = ImageModel(
    key="sd15", title="Stable Diffusion 1.5 (fp16)", repo="stable-diffusion-v1-5/stable-diffusion-v1-5",
    revision="451f4fe16113bff5a5d2269ed5ad43b0592e9a14", native=512, license="CreativeML OpenRAIL-M",
    size_mb=2300, note="Rápido; cabe holgado en 8 GB. Resolución nativa 512×512.",
    files={
        "model_index.json": "", "scheduler/scheduler_config.json": "",
        "text_encoder/config.json": "",
        "text_encoder/model.fp16.safetensors": "77795e2023adcf39bc29a884661950380bd093cf0750a966d473d1718dc9ef4e",
        "tokenizer/merges.txt": "", "tokenizer/special_tokens_map.json": "", "tokenizer/tokenizer_config.json": "",
        "tokenizer/vocab.json": "",
        "unet/config.json": "",
        "unet/diffusion_pytorch_model.fp16.safetensors": "c83908253f9a64d08c25fc90874c9c8aef9a329ce1ca5fb909d73b0c83d1ea21",
        "vae/config.json": "",
        "vae/diffusion_pytorch_model.fp16.safetensors": "4fbcf0ebe55a0984f5a5e00d8c4521d52359af7229bb4d81890039d2aa16dd7c",
    })

_SDXL = ImageModel(
    key="sdxl", title="Stable Diffusion XL base 1.0 (fp16)", repo="stabilityai/stable-diffusion-xl-base-1.0",
    revision="462165984030d82259a11f4367a4eed129e94a7b", native=1024, license="CreativeML OpenRAIL++-M", xl=True,
    size_mb=6900, note="Más calidad; con 8 GB va con descarga parcial a la CPU (más lento). Nativa 1024×1024.",
    files={
        "model_index.json": "", "scheduler/scheduler_config.json": "",
        "text_encoder/config.json": "",
        "text_encoder/model.fp16.safetensors": "660c6f5b1abae9dc498ac2d21e1347d2abdb0cf6c0c0c8576cd796491d9a6cdd",
        "text_encoder_2/config.json": "",
        "text_encoder_2/model.fp16.safetensors": "ec310df2af79c318e24d20511b601a591ca8cd4f1fce1d8dff822a356bcdb1f4",
        "tokenizer/merges.txt": "", "tokenizer/special_tokens_map.json": "", "tokenizer/tokenizer_config.json": "",
        "tokenizer/vocab.json": "",
        "tokenizer_2/merges.txt": "", "tokenizer_2/special_tokens_map.json": "", "tokenizer_2/tokenizer_config.json": "",
        "tokenizer_2/vocab.json": "",
        "unet/config.json": "",
        "unet/diffusion_pytorch_model.fp16.safetensors": "83e012a805b84c7ca28e5646747c90a243c65c8ba4f070e2d7ddc9d74661e139",
        "vae/config.json": "",
        "vae/diffusion_pytorch_model.fp16.safetensors": "bcb60880a46b63dea58e9bc591abe15f8350bde47b405f9c38f4be70c6161e68",
    })

CATALOG: dict[str, ImageModel] = {m.key: m for m in (_SD15, _SDXL)}


class HashMismatch(RuntimeError):
    pass


def is_installed(model: ImageModel) -> bool:
    """Instalado = todos los archivos del catálogo existen (la integridad se comprueba al descargar)."""
    return all((model.dir / rel).is_file() for rel in model.files)


def verify(model: ImageModel, progress: Callable[[str], None] | None = None) -> list[str]:
    """Comprueba el SHA-256 de cada peso. Devuelve la lista de archivos que NO coinciden (vacía = todo bien)."""
    bad = []
    for rel, want in model.files.items():
        if not want:
            continue
        f = model.dir / rel
        if progress:
            progress(f"Comprobando {rel}…")
        if not f.is_file() or sha256_file(f) != want:
            bad.append(rel)
    return bad


def download(model: ImageModel, progress: Callable[[str], None] | None = None) -> Path:
    """Descarga el modelo (SOLO tras la confirmación del usuario) y verifica los hashes.

    Si un hash no coincide, borra ese archivo y lanza HashMismatch. Solo se piden los archivos del catálogo."""
    from huggingface_hub import hf_hub_download
    model.dir.mkdir(parents=True, exist_ok=True)
    with allow_network(f"descarga confirmada de {model.repo}"):
        return _download_files(model, hf_hub_download, progress)


def _download_files(model: ImageModel, hf_hub_download, progress) -> Path:
    for rel in model.files:
        dest = model.dir / rel
        want = model.files[rel]
        if dest.is_file() and (not want or sha256_file(dest) == want):
            continue
        if progress:
            progress(f"Descargando {rel}…")
        got = Path(hf_hub_download(model.repo, rel, revision=model.revision, local_dir=str(model.dir)))
        if want and sha256_file(got) != want:
            got.unlink(missing_ok=True)
            raise HashMismatch(f"El archivo {rel} descargado no coincide con el hash esperado; se ha borrado.")
    if progress:
        progress("Listo: hashes verificados.")
    return model.dir


def remove(model: ImageModel):
    if model.dir.exists():
        shutil.rmtree(model.dir)
