"""Cache de resúmenes y de resultados parciales (por hash del PDF + parámetros)."""
from __future__ import annotations

import hashlib
import json
import logging

from .utils import CACHE_DIR

log = logging.getLogger("resumidor")


def params_hash(params: dict) -> str:
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _path(pdf_hash: str, kind: str, phash: str):
    return CACHE_DIR / f"{pdf_hash[:20]}_{kind}_{phash}.json"


def load(pdf_hash: str, kind: str, phash: str):
    """Devuelve el contenido cacheado o None."""
    p = _path(pdf_hash, kind, phash)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("Cache corrupta %s: %s", p.name, exc)
        return None


def save(pdf_hash: str, kind: str, phash: str, data) -> None:
    p = _path(pdf_hash, kind, phash)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
