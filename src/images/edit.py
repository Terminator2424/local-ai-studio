"""Edición de imágenes con Pillow: recorte, giros, tamaño y filtros con parámetros (para sliders con vista previa).

Todas las funciones son puras (devuelven una imagen nueva) y no usan GPU: se pueden probar sin modelos.
Orden de aplicación: recorte → giro → volteo → tamaño → filtros.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

SAVE_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
MAX_SIDE = 16384        # tope de lado para no agotar la memoria con un resize descontrolado


@dataclass
class EditParams:
    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)   # izquierda, arriba, derecha, abajo (fracciones 0-1)
    rotate: int = 0                       # 0, 90, 180 o 270 (sentido horario)
    flip_h: bool = False
    flip_v: bool = False
    resize: tuple[int, int] | None = None  # (ancho, alto) en píxeles; None = sin cambio
    grayscale: bool = False
    blur: float = 0.0                     # radio del desenfoque gaussiano (0 = ninguno)
    sepia: float = 0.0                    # 0-1
    sharpen: float = 0.0                  # 0-2 (0 = ninguno)
    brightness: float = 1.0               # 1 = igual
    contrast: float = 1.0
    saturation: float = 1.0
    extra: dict = field(default_factory=dict)

    def is_identity(self) -> bool:
        return self == EditParams()


def _crop(img: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    l, t, r, b = box
    if not (0 <= l < r <= 1 and 0 <= t < b <= 1):
        raise ValueError("El recorte debe cumplir 0 ≤ izquierda < derecha ≤ 1 y 0 ≤ arriba < abajo ≤ 1.")
    w, h = img.size
    px = (round(l * w), round(t * h), max(round(r * w), round(l * w) + 1), max(round(b * h), round(t * h) + 1))
    return img.crop(px) if px != (0, 0, w, h) else img


def _sepia(img: Image.Image, strength: float) -> Image.Image:
    tone = ImageOps.colorize(ImageOps.grayscale(img), black="#2b1d0e", white="#ffe9c7", mid="#a07a4c").convert("RGB")
    return Image.blend(img.convert("RGB"), tone, max(0.0, min(1.0, strength)))


def apply(img: Image.Image, p: EditParams) -> Image.Image:
    """Aplica todos los ajustes y devuelve una imagen nueva (la original no se modifica)."""
    out = ImageOps.exif_transpose(img) or img
    alpha = out.getchannel("A") if out.mode in ("RGBA", "LA") else None
    out = out.convert("RGB")
    out = _crop(out, p.crop)
    if p.rotate % 360:
        if p.rotate % 90:
            raise ValueError("El giro debe ser múltiplo de 90°.")
        out = out.rotate(-(p.rotate % 360), expand=True)
    if p.flip_h:
        out = ImageOps.mirror(out)
    if p.flip_v:
        out = ImageOps.flip(out)
    if p.resize is not None:
        w, h = p.resize
        if not (1 <= w <= MAX_SIDE and 1 <= h <= MAX_SIDE):
            raise ValueError(f"El tamaño debe estar entre 1 y {MAX_SIDE} píxeles por lado.")
        out = out.resize((int(w), int(h)), Image.LANCZOS)
    if p.grayscale:
        out = ImageOps.grayscale(out).convert("RGB")
    if p.sepia > 0:
        out = _sepia(out, p.sepia)
    if p.saturation != 1.0:
        out = ImageEnhance.Color(out).enhance(p.saturation)
    if p.brightness != 1.0:
        out = ImageEnhance.Brightness(out).enhance(p.brightness)
    if p.contrast != 1.0:
        out = ImageEnhance.Contrast(out).enhance(p.contrast)
    if p.blur > 0:
        out = out.filter(ImageFilter.GaussianBlur(p.blur))
    if p.sharpen > 0:
        out = ImageEnhance.Sharpness(out).enhance(1.0 + p.sharpen * 2)
    if alpha is not None and out.size == alpha.size:
        out.putalpha(alpha)
    return out


def preview(img: Image.Image, p: EditParams, max_side: int = 720) -> Image.Image:
    """Vista previa rápida: aplica los ajustes sobre una copia reducida (el resize en píxeles se omite)."""
    small = img.copy()
    small.thumbnail((max_side, max_side))
    q = EditParams(**{**p.__dict__, "resize": None})
    return apply(small, q)


def fit_size(size: tuple[int, int], w: int | None = None, h: int | None = None) -> tuple[int, int]:
    """Tamaño que conserva la proporción dado el ancho o el alto deseado."""
    ow, oh = size
    if w and not h:
        return w, max(1, round(oh * w / ow))
    if h and not w:
        return max(1, round(ow * h / oh)), h
    return w or ow, h or oh


def save(img: Image.Image, path: str | Path, fmt: str | None = None, quality: int = 92) -> Path:
    """Guarda en PNG, JPEG o WebP. Si el formato no se indica, se deduce de la extensión."""
    path = Path(path)
    fmt = (fmt or {v: k for k, v in SAVE_FORMATS.items()}.get(path.suffix.lower(), "PNG")).upper()
    if fmt not in SAVE_FORMATS:
        raise ValueError(f"Formato no admitido: {fmt}. Usa {', '.join(SAVE_FORMATS)}.")
    path = path.with_suffix(SAVE_FORMATS[fmt])
    path.parent.mkdir(parents=True, exist_ok=True)
    out = img
    if fmt == "JPEG" and img.mode in ("RGBA", "LA", "P"):
        out = img.convert("RGB")
    out.save(path, format=fmt, **({"quality": quality} if fmt in ("JPEG", "WEBP") else {}))
    return path


def load(path: str | Path) -> Image.Image:
    """Abre una imagen validando que lo sea y limitando su tamaño (evita bombas de descompresión)."""
    with Image.open(path) as im:
        im.load()
        if im.width * im.height > 120_000_000:
            raise ValueError("La imagen es demasiado grande (más de 120 megapíxeles).")
        return im.copy()
