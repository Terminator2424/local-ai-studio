"""Utilidades de texto: plantillas, nombres de archivo, tiempos, hashes y detección de idioma."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


def sha256_file(path: str | Path, chunk: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m:02d}m {s:02d}s"


def fill(template: str, **values) -> str:
    """Sustituye {clave} sin tocar otras llaves del texto (a diferencia de str.format)."""
    for key, val in values.items():
        template = template.replace("{" + key + "}", str(val))
    return template


def safe_name(name: str) -> str:
    """Nombre de archivo seguro en Windows: sin caracteres reservados ni espacios, máximo 80 caracteres."""
    return re.sub(r'[<>:"/\\|?*\s]+', "_", name).strip("_")[:80] or "documento"


# --------------------------------------------------------------- idioma
_STOPWORDS = {
    "es": "de la que el en y los del se las por un para con una su al lo como más pero sus ya este sí porque esta entre "
          "cuando muy sin sobre también hasta hay donde desde todo nos durante son fue ser",
    "en": "the of and to in is that for it as was with be by on not this are or his from at which but have an had they "
          "you were their all we can her has there been if more when will would who so than into also these",
    "fr": "le la les des du de et un une que qui dans pour pas sur avec est sont au aux ce cette ces plus par mais ou ne "
          "il elle nous vous leur être avoir comme",
    "pt": "o os as um uma de da do das dos que em para com não por mais como mas foi são ao aos se sua seu também entre "
          "sobre quando muito já está",
    "de": "der die das und ist von zu den mit sich des auf für nicht ein eine dem im als auch es an werden aus er hat "
          "dass sie nach bei um am sind noch wie einem",
    "it": "il lo la i gli le di che e un una per con non sono del della dei delle più come ma anche è si da nel nella al "
          "alla questo questa",
}
_STOPSETS = {code: frozenset(words.split()) for code, words in _STOPWORDS.items()}
LANG_NAMES_ES = {"es": "español", "en": "inglés", "fr": "francés", "pt": "portugués", "de": "alemán", "it": "italiano"}
LANG_NAMES_EN = {"es": "Spanish", "en": "English", "fr": "French", "pt": "Portuguese", "de": "German", "it": "Italian"}


def detect_language(text: str) -> str:
    """Detección ligera por frecuencia de palabras vacías (sin dependencias extra). Por defecto, español."""
    words = re.findall(r"[a-záéíóúñüàâçèêëîïôùûœãõß]+", text[:30000].lower())
    if not words:
        return "es"
    best, best_score = "es", -1.0
    for code, stops in _STOPSETS.items():
        score = sum(1 for w in words if w in stops) / len(words)
        if score > best_score:
            best, best_score = code, score
    return best
