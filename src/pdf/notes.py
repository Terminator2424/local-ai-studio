"""Utilidades para las notas por fragmento: normalización Unicode, secciones canónicas, tokens y deduplicación."""
from __future__ import annotations

import re
import unicodedata

from .textproc import _EMPTY_RE

_KEYS = (("ideas", ("ideas principales", "main ideas")),
         ("data", ("datos", "data")),
         ("formulas", ("fórmulas", "formulas", "fórmula")),
         ("statements", ("enunciados", "statements")),
         ("conclusions", ("conclusiones", "conclusions")),
         ("figures", ("figuras", "figures")))


def nfc(text: str) -> str:
    """Unicode NFC: «o» + «´» se componen en «ó», así las tildes se ven bien en cualquier visor."""
    return unicodedata.normalize("NFC", text)


def est_tokens(text: str) -> int:
    """Estimación rápida de tokens de un LLM (~3.6 caracteres por token en español/inglés con fórmulas)."""
    return int(len(text) / 3.6)


def parse_note(note: str) -> dict[str, str]:
    """Separa una nota en secciones canónicas {ideas, data, formulas, statements, conclusions, figures}.
    Las secciones vacías («—», «No hay…») no se incluyen."""
    parts = re.split(r"^#{3,4}\s+(.+?)\s*$", note, flags=re.M)
    out: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        label = parts[i].strip(" *").lower()
        body = parts[i + 1].strip()
        lines = [ln for ln in body.splitlines() if ln.strip()]
        if not lines or all(_EMPTY_RE.match(ln.strip()) for ln in lines):
            continue
        key = next((k for k, names in _KEYS if label.startswith(names)), None)
        if key is None:
            key = "ideas"
        out[key] = (out[key] + "\n" + body) if key in out else body
    return out


def norm_key(text: str) -> str:
    return re.sub(r"[\s{}]+|\\(?:left|right|,|;|quad)", "", text.lower())


def unique_lines(body: str, seen: set[str], min_len: int = 12, prose_only: bool = False) -> str:
    """Quita las líneas ya vistas (el solape entre fragmentos las repite).
    prose_only=True: solo se deduplican líneas largas de texto (nunca fórmulas, tablas ni enunciados cortos),
    para no romper frases ni listas numeradas que legítimamente se repiten entre problemas."""
    out = []
    for ln in body.splitlines():
        s = ln.lstrip()
        if s.startswith("|") or (prose_only and (s.startswith(("$", "\\[", "\\(")) or re.match(r"\d+\.\s", s))):
            out.append(ln)       # las filas de tablas pueden repetirse legítimamente (p. ej. matriz de pesos)
            continue
        k = norm_key(ln)
        if len(k) >= min_len and k in seen:
            continue
        if len(k) >= min_len:
            seen.add(k)
        out.append(ln)
    return "\n".join(out).strip()
