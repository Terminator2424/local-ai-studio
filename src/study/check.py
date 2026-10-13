"""Comprobación SIN IA de lo que escribe el modelo al estudiar: cada fórmula con igualdades se marca como
verificada (✓), incorrecta (✗) o sin verificar (⚠). Lo que el verificador no puede comprobar se dice, no se da por bueno.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..pdf.symcheck import verify_chain
from ..pdf.verify import numeric_chain_error

_MATH = re.compile(r"(\$\$(.+?)\$\$)|(\\\[(.+?)\\\])|((?<![\\$])\$([^$\n]+?)\$)", re.S)
_CODE = re.compile(r"```([A-Za-z0-9_+-]*)[ \t]*\n(.*?)```", re.S)


@dataclass
class Stats:
    ok: int = 0
    error: int = 0
    unknown: int = 0

    def summary(self) -> str:
        total = self.ok + self.error + self.unknown
        if not total:
            return "Sin igualdades que comprobar."
        return f"Igualdades: {self.ok} verificadas ✓ · {self.error} incorrectas ✗ · {self.unknown} sin verificar ⚠ (de {total})"


def chain_state(latex: str) -> tuple[str, str]:
    """('ok'|'error'|'unknown', motivo). Primero la aritmética exacta, después el verificador simbólico."""
    err = numeric_chain_error(latex)
    if err:
        return "error", err
    return verify_chain(latex)


def annotate(md: str) -> tuple[str, Stats]:
    """Añade ✓ / ✗ / ⚠ detrás de cada fórmula con igualdades."""
    stats = Stats()

    def rep(m):
        body = next(g for g in (m.group(2), m.group(4), m.group(6)) if g is not None)
        if not re.search(r"=|\\approx|≈", body):
            return m.group(0)
        state, why = chain_state(body)
        if state == "ok":
            stats.ok += 1
            return m.group(0) + " ✓"
        if state == "error":
            stats.error += 1
            return m.group(0) + f" ✗ {why}"
        stats.unknown += 1
        return m.group(0) + " ⚠ sin verificar"
    return _MATH.sub(rep, md), stats


def code_blocks(md: str) -> list[tuple[str, str]]:
    """Bloques de código del Markdown: [(lenguaje, código)]."""
    return [(m.group(1).lower(), m.group(2).rstrip()) for m in _CODE.finditer(md)]
