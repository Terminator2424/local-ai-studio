"""Texto de una página con las TABLAS reconstruidas (a partir de la posición de las palabras).

PyMuPDF entrega cada celda de una tabla como una línea suelta; aquí se agrupan las palabras por fila visual y se
separan en celdas por los huecos horizontales. Solo se considera tabla un tramo de ≥3 filas consecutivas con el
mismo número de celdas (≥2), para no confundir código, fórmulas o prosa con tablas.
"""
from __future__ import annotations

import re
from itertools import pairwise

import pymupdf

_CODE_HINT = re.compile(r"[{}]|;\s*$|=>|===|!==|\b(?:function|if|for|while|switch)\s*\(|\b(?:let|const|var|return)\s+\w")
_FORMULA_CHARS = set("∫∑∏≈≤≥√∞∂∈→")


def _rows(words: list) -> list[dict]:
    """Agrupa palabras en filas visuales (misma altura) y las divide en celdas por huecos grandes."""
    words = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows: list[dict] = []
    for w in words:
        yc = (w[1] + w[3]) / 2
        h = w[3] - w[1]
        if rows and abs(rows[-1]["yc"] - yc) <= max(2.5, 0.45 * h):
            rows[-1]["words"].append(w)
        else:
            rows.append({"yc": yc, "y0": w[1], "y1": w[3], "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w[0])
        r["y0"] = min(w[1] for w in r["words"])
        r["y1"] = max(w[3] for w in r["words"])
        cells: list[list] = [[r["words"][0]]]
        for prev, w in pairwise(r["words"]):
            size = max(prev[3] - prev[1], 6.0)
            if w[0] - prev[2] > max(8.0, 0.9 * size):   # hueco claramente mayor que un espacio
                cells.append([w])
            else:
                cells[-1].append(w)
        r["cells"] = [" ".join(x[4] for x in c) for c in cells]
        r["cell_x"] = [c[0][0] for c in cells]
        r["text"] = " ".join(w[4] for w in r["words"])
    return rows


def _tabular(r: dict) -> bool:
    cells = r["cells"]
    if len(cells) < 2 or max(len(c) for c in cells) > 70:
        return False
    return not (_CODE_HINT.search(r["text"]) or any(ch in _FORMULA_CHARS for ch in r["text"]))


def _find_tables(rows: list[dict]) -> list[tuple[int, int]]:
    """Tramos [i, j) de filas consecutivas con el mismo nº de celdas (≥2) y columnas alineadas."""
    tables, i = [], 0
    while i < len(rows):
        if not _tabular(rows[i]):
            i += 1
            continue
        n = len(rows[i]["cells"])
        j = i + 1
        while j < len(rows) and _tabular(rows[j]) and len(rows[j]["cells"]) == n and \
                rows[j]["y0"] - rows[j - 1]["y1"] < 3.2 * max(rows[j]["y1"] - rows[j]["y0"], 8):
            j += 1
        if j - i >= 3:
            tables.append((i, j))
        i = max(j, i + 1)
    return tables


def _table_md(rows: list[dict]) -> str:
    lines = ["| " + " | ".join(c.replace("|", "\\|") for c in rows[0]["cells"]) + " |",
             "|" + "---|" * len(rows[0]["cells"])]
    lines += ["| " + " | ".join(c.replace("|", "\\|") for c in r["cells"]) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _gutter(page: pymupdf.Page) -> float | None:
    """x del canal entre dos columnas de texto, o None si la página es de una sola columna.
    Hay dos columnas si existe una franja vertical (≥ 14 pt) cerca del centro por la que no cruza casi ninguna palabra
    y a ambos lados hay texto en abundancia."""
    words = page.get_text("words")
    if len(words) < 80:
        return None
    width = page.rect.width
    lo, hi = width * 0.35, width * 0.65
    step = 2.0
    best, run_start, best_w = None, None, 0.0
    x = lo
    while x <= hi:
        crossing = sum(1 for w in words if w[0] < x < w[2])
        if crossing <= max(1, 0.01 * len(words)):
            run_start = x if run_start is None else run_start
            if x - run_start > best_w:
                best_w, best = x - run_start, (run_start + x) / 2
        else:
            run_start = None
        x += step
    if best is None or best_w < 14:
        return None
    left = sum(1 for w in words if w[2] <= best)
    right = sum(1 for w in words if w[0] >= best)
    if min(left, right) < 0.25 * len(words):
        return None
    # las líneas largas que cruzan el canal (títulos a todo el ancho) no deben ser mayoría
    return best


def layout_text(page: pymupdf.Page) -> str:
    """Texto de la página en orden de lectura (una o dos columnas) con las tablas como Markdown."""
    gutter = _gutter(page)
    if gutter is not None:
        w, h = page.rect.width, page.rect.height
        cols = []
        for rect in (pymupdf.Rect(0, 0, gutter, h), pymupdf.Rect(gutter, 0, w, h)):
            cols.append(_layout_region(page, rect))
        return "\n\n".join(c for c in cols if c.strip())
    return _layout_region(page, page.rect)


def _layout_region(page: pymupdf.Page, region: pymupdf.Rect) -> str:
    """Texto de una región (columna o página) con las tablas que contiene reconstruidas."""
    plain = page.get_text("text", clip=region, sort=True)
    try:
        words = list(page.get_text("words", clip=region))
        rows = _rows(words)
        tables = _find_tables(rows)
    except Exception:
        return plain
    if not tables:
        return plain
    out: list[str] = []
    cursor = region.y0
    for i, j in tables:
        y0, y1 = rows[i]["y0"] - 1.0, rows[j - 1]["y1"] + 1.0
        if y0 > cursor:
            out.append(page.get_text("text", clip=pymupdf.Rect(region.x0, cursor, region.x1, y0), sort=True))
        out.append("\n" + _table_md(rows[i:j]) + "\n")
        cursor = y1
    if cursor < region.y1:
        out.append(page.get_text("text", clip=pymupdf.Rect(region.x0, cursor, region.x1, region.y1), sort=True))
    return "\n".join(out)
