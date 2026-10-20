"""Uso de disco de la aplicación y limpieza SEGURA de lo que se puede volver a generar.

Solo se borran la caché de resúmenes y las páginas de PDF renderizadas para la visión: son derivadas y se recrean
solas. Nunca se tocan tus resúmenes, chats, índice de documentos, imágenes generadas ni modelos.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .paths import BASE_DIR, CACHE_DIR, IMAGES_DIR, LOG_DIR, SUMMARIES_DIR

log = logging.getLogger(__name__)

MB = 1024 * 1024
PROTECTED = frozenset({"queue.json", "ui.json"})     # estado de la interfaz que vive en la carpeta de caché: nunca se poda
CACHE_LIMIT = 1024 * MB          # la poda automática deja cada carpeta regenerable por debajo de este tamaño
PAGES_LIMIT = 1024 * MB


@dataclass(frozen=True)
class Area:
    key: str
    title: str
    path: Path
    note: str
    clearable: bool = False      # solo las regenerables


AREAS = (
    Area("models", "Modelos de imágenes", BASE_DIR / "models", "Descargados y verificados con SHA-256."),
    Area("outputs", "Imágenes generadas", BASE_DIR / "outputs", "Tus imágenes (el prompt y la semilla van dentro del PNG)."),
    Area("chats", "Chats e índice de documentos", BASE_DIR / "conversations", "Historial y búsqueda de «Documentos»."),
    Area("summaries", "Resúmenes guardados", SUMMARIES_DIR, "Tus resúmenes en Markdown y Word."),
    Area("transcriptions", "Transcripciones de audio", BASE_DIR / "transcripciones", "Tus transcripciones guardadas en formato .txt."),
    Area("cache", "Caché de resúmenes", CACHE_DIR, "Se recrea sola; ahorra repetir el trabajo con un PDF ya resumido.", True),
    Area("pages", "Páginas de PDF renderizadas", IMAGES_DIR, "Imágenes temporales para el modelo de visión.", True),
    Area("logs", "Registros", LOG_DIR, "app.log y sus copias rotadas."),
)
_BY_KEY = {a.key: a for a in AREAS}


def folder_size(path: Path) -> int:
    """Bytes de todos los archivos bajo `path` (sin seguir enlaces, ignorando lo que no se pueda leer)."""
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def usage() -> list[tuple[Area, int]]:
    # «summaries» incluye la caché; se descuenta para que cada byte se cuente una sola vez
    sizes = {a.key: folder_size(a.path) for a in AREAS}
    sizes["summaries"] = max(0, sizes["summaries"] - sizes["cache"])
    return [(a, sizes[a.key]) for a in AREAS]


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _files(path: Path) -> list[tuple[float, int, Path]]:
    out = []
    for dirpath, _, names in os.walk(path):
        for name in names:
            p = Path(dirpath, name)
            if name in PROTECTED:
                continue
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                st = p.stat()
            except OSError:
                continue
            out.append((st.st_mtime, st.st_size, p))
    return out


def _inside_app(path: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(BASE_DIR))
    except ValueError:
        return False
    return True


def _remove_empty_dirs(path: Path) -> None:
    for dirpath, _, _ in os.walk(path, topdown=False):
        if Path(dirpath) != path:
            try:
                os.rmdir(dirpath)             # solo si está vacía
            except OSError:
                continue


def prune(path: Path, max_bytes: int) -> int:
    """Borra los archivos MÁS ANTIGUOS de `path` hasta dejarla por debajo de `max_bytes`. Devuelve los bytes liberados."""
    if not _inside_app(path):
        raise ValueError(f"Por seguridad solo se limpian carpetas de la aplicación: {path}")
    files = sorted(_files(path))                                    # más antiguos primero
    total = sum(size for _, size, _ in files)
    freed = 0
    for _, size, p in files:
        if total - freed <= max_bytes:
            break
        try:
            p.unlink()
            freed += size
        except OSError as exc:
            log.warning("No se pudo borrar %s: %s", p, exc)
    if freed:
        _remove_empty_dirs(path)
    return freed


def clear(key: str) -> int:
    """Vacía una carpeta regenerable («cache» o «pages»). Cualquier otra se rechaza. Devuelve los bytes liberados."""
    area = _BY_KEY.get(key)
    if area is None or not area.clearable:
        raise ValueError(f"«{key}» no se puede limpiar desde la aplicación.")
    return prune(area.path, 0)


def auto_prune() -> int:
    """Mantiene las carpetas regenerables por debajo de su tope. Se ejecuta al arrancar, en segundo plano."""
    freed = 0
    for path, limit in ((CACHE_DIR, CACHE_LIMIT), (IMAGES_DIR, PAGES_LIMIT)):
        try:
            freed += prune(path, limit)
        except (OSError, ValueError) as exc:
            log.warning("Poda automática de %s omitida: %s", path.name, exc)
    if freed:
        log.info("Poda automática: %s liberados", human(freed))
    return freed
