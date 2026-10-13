"""Tercera lectura de fórmulas con un OCR matemático especializado (pix2tex / LaTeX-OCR), opcional.

pix2tex lee muy bien los SÍMBOLOS (dígitos, letras, operadores) de una fórmula renderizada, aunque a veces confunde la
estructura (p. ej. una fracción como \\underbrace). Por eso se usa para contrastar los símbolos de lo que leyó la visión:
si a la visión le sobra o le falta un dígito, una letra o un signo respecto a pix2tex, la fórmula se marca con ⚠.

Instalación (opcional, corre en CPU, no usa VRAM):
    pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
    pip install pix2tex
"""
from __future__ import annotations

import io
import logging
import re
from collections import Counter

import pymupdf

from .layout import _find_tables, _rows

log = logging.getLogger(__name__)

_model = None
_unavailable = False


def available() -> bool:
    """True si pix2tex está instalado (no lo carga todavía)."""
    global _unavailable
    if _unavailable:
        return False
    try:
        import importlib.util
        ok = importlib.util.find_spec("pix2tex") is not None and importlib.util.find_spec("torch") is not None
    except Exception:  # noqa: BLE001
        ok = False
    _unavailable = not ok
    return ok


def _load():
    global _model, _unavailable
    if _model is None and not _unavailable:
        try:
            import warnings
            warnings.filterwarnings("ignore")
            from pix2tex.cli import LatexOCR
            _model = LatexOCR()
        except Exception as exc:  # noqa: BLE001
            log.warning("No se pudo cargar pix2tex (se omite la tercera lectura): %s", exc)
            _unavailable = True
    return _model


# ------------------------------------------------------------------ símbolos
_STRUCT = {"\\frac", "\\sqrt", "\\limits", "\\underbrace", "\\overbrace", "\\left", "\\right", "\\qquad", "\\quad", "\\displaystyle",
           "\\mathrm", "\\text", "\\textbf", "\\mathbf", "\\operatorname", "\\mathit", "\\boldsymbol", "\\begin", "\\end", "\\hline",
           "\\cdot", "\\times", "\\,", "\\;", "\\!", "\\:", "\\dfrac", "\\tfrac"}


def symbol_tokens(latex: str) -> Counter:
    """Multiconjunto de símbolos de una fórmula (sin la estructura): dígitos, letras, operadores y comandos de símbolo."""
    s = latex.replace("\\{", "{").replace("\\}", "}")
    toks = re.findall(r"\\[A-Za-z]+|\d+(?:\.\d+)?|[A-Za-z]|[+\-−=<>≈≤≥]", s)
    out: Counter = Counter()
    for t in toks:
        t = "-" if t == "−" else t
        if t in _STRUCT:
            continue
        out[t] += 1
    return out


# ------------------------------------------------------ zonas de fórmula en la página
def _formula_row(r: dict) -> bool:
    txt = r["text"]
    letters = re.findall(r"[A-Za-záéíóúñÁÉÍÓÚÑ]{4,}", txt)
    return not letters and len(txt) < 120


def formula_blocks(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Rectángulos de las fórmulas en bloque: filas consecutivas sin palabras (símbolos, números sueltos, límites)."""
    rows = _rows(page.get_text("words"))
    table_rows: set[int] = set()
    for i, j in _find_tables(rows):
        table_rows.update(range(i, j))
    blocks: list[list[dict]] = []
    for idx, r in enumerate(rows):
        if idx in table_rows or not _formula_row(r):
            continue
        h = max(r["y1"] - r["y0"], 8)
        if blocks and r["y0"] - blocks[-1][-1]["y1"] <= 2.2 * h:
            blocks[-1].append(r)
        else:
            blocks.append([r])
    rects = []
    for b in blocks:
        if sum(len(r["text"]) for r in b) < 6:
            continue
        x0 = min(w[0] for r in b for w in r["words"]) - 8
        x1 = max(w[2] for r in b for w in r["words"]) + 8
        rects.append(pymupdf.Rect(max(0, x0), b[0]["y0"] - 5, min(page.rect.width, x1), b[-1]["y1"] + 5))
    return rects


def read_blocks(page: pymupdf.Page, dpi: int = 300) -> list[str]:
    """LaTeX leído por pix2tex en cada zona de fórmula de la página."""
    model = _load()
    if model is None:
        return []
    from PIL import Image
    out = []
    for rect in formula_blocks(page):
        try:
            pix = page.get_pixmap(dpi=dpi, clip=rect)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            out.append(model(img))
        except Exception as exc:  # noqa: BLE001
            log.info("pix2tex falló en una zona: %s", exc)
            out.append("")
    return out


def third_reading(page: pymupdf.Page, vision_eqs: list[str], dpi: int = 300) -> dict[int, str]:
    """Contrasta los símbolos de cada fórmula de la visión con pix2tex. Devuelve {índice: motivo} de las discrepancias.
    Solo se opina cuando pix2tex tiene una zona que corresponde a la fórmula (≥ 55 % de símbolos en común)."""
    blocks = [symbol_tokens(b) for b in read_blocks(page, dpi) if b]
    if not blocks:
        return {}
    issues: dict[int, str] = {}
    for k, eq in enumerate(vision_eqs):
        mine = symbol_tokens(eq)
        if sum(mine.values()) < 6:
            continue
        best, best_j = None, 0.0
        for b in blocks:
            inter = sum((mine & b).values())
            union = sum((mine | b).values())
            j = inter / union if union else 0.0
            if j > best_j:
                best, best_j = b, j
        if best is None or best_j < 0.55:
            continue
        extra = mine - best                      # símbolos que la visión tiene y pix2tex no
        if extra:
            shown = ", ".join(f"«{t}»" + (f"×{c}" if c > 1 else "") for t, c in list(extra.items())[:5])
            issues[k] = f"la tercera lectura (pix2tex) no ve: {shown}"
    return issues
