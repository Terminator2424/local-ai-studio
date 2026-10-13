"""Extracción: PyMuPDF -> pdfplumber -> visión (OCR y páginas con fórmulas) e imágenes de páginas."""
from __future__ import annotations

import logging
import re
import threading
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf

from .layout import layout_text
from .textproc import extract_equations, fix_accents
from . import mathgeom, mathocr
from .verify import compare_readings, operator_audit, diff_lines
from ..core.utils import IMAGES_DIR, Cancelled, PDFError

log = logging.getLogger("resumidor")

_PAGENUM_RE = re.compile(r"^\s*(p[áa]g(ina)?\.?|page)?\s*\d+(\s*(de|of|/)\s*\d+)?\s*$", re.I)
_MATH_CHARS = set("∫∑∏≈≤≥≠±√∞∂∈→−×÷∇∮")
_ORPHAN_RE = re.compile(r"^(Z{1,3}|[0-9]{1,2}|[A-Za-z]{1,2})$")
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class PageData:
    number: int          # 1-based
    text: str            # texto final usado para resumir
    source: str          # 'texto' | 'texto+fórmulas' | 'pdfplumber' | 'visión' | 'vacía'
    raw: str = ""        # texto vectorial original (para contexto de figuras)
    eqs: list = field(default_factory=list)   # [[posición relativa, latex], ...] leídas de la imagen
    vis: str = ""        # transcripción completa de la visión (solo páginas con fórmulas)
    eq_flags: list = field(default_factory=list)    # por fórmula: {"ok": bool, "why": [...], "alt": "lectura alternativa"}
    warns: list = field(default_factory=list)       # avisos de la página (operadores de más, líneas dudosas de OCR...)
    geo: list = field(default_factory=list)         # fórmulas reconstruidas desde los glifos del PDF (mathgeom.GeoEq)


def open_pdf(path: str | Path) -> pymupdf.Document:
    """Abre un PDF gestionando archivos corruptos o cifrados con mensajes claros."""
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:  # noqa: BLE001
        raise PDFError(f"No se pudo abrir el PDF (¿corrupto?): {exc}") from exc
    if doc.needs_pass:
        if not doc.authenticate(""):
            doc.close()
            raise PDFError("El PDF está cifrado con contraseña. Quita la protección e inténtalo de nuevo.")
    if doc.page_count == 0:
        doc.close()
        raise PDFError("El PDF no contiene páginas.")
    return doc


def render_page(doc: pymupdf.Document, page_index: int, dpi: int, pdf_hash: str) -> Path:
    """Renderiza una página a PNG en imagenes/<hash>/ (reutiliza si ya existe)."""
    out_dir = IMAGES_DIR / pdf_hash[:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"p{page_index + 1:04d}_{dpi}.png"
    if not out.exists():
        pix = doc[page_index].get_pixmap(dpi=dpi, alpha=False)
        pix.save(str(out))
    return out


def _clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = fix_accents(text)
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"-\n(?=[a-záéíóúñ])", "", text)          # une palabras cortadas por guion
    text = "\n".join(ln.rstrip() for ln in text.splitlines())
    text = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", text)     # espacios internos; la sangría (código) se conserva
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def _clean_vision(text: str) -> str:
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text.strip())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def math_score(text: str) -> int:
    """Puntúa cuánta matemática hay: símbolos matemáticos + líneas «huérfanas» (numeradores/denominadores)."""
    score = sum(1 for c in text if c in _MATH_CHARS)
    for ln in text.splitlines():
        if _ORPHAN_RE.match(ln.strip()):
            score += 1
    # El símbolo de integral se extrae como «Z» pegado a sus límites («Z 8 Z 7», «ZZ»): señal inequívoca de fórmula
    score += 2 * len(re.findall(r"(?:^|\s)Z{1,3}\s+[-\d]", text))
    return score


def _strip_headers_footers(texts: list[str]) -> list[str]:
    """Quita líneas que se repiten en muchas páginas (encabezados/pies) y números de página."""
    n = len(texts)
    if n >= 5:
        counter: Counter = Counter()
        for t in texts:
            counter.update({ln.strip() for ln in t.splitlines() if 3 <= len(ln.strip()) <= 90})
        frequent = {ln for ln, c in counter.items() if c >= max(3, int(n * 0.4))}
    else:
        frequent = set()
    out = []
    for k, t in enumerate(texts):
        lines = t.splitlines()
        nonblank = [i for i, ln in enumerate(lines) if ln.strip()]
        edge = set(nonblank[:3] + nonblank[-3:])        # cabecera y pie de la página
        kept = []
        for i, ln in enumerate(lines):
            if ln.strip() in frequent:
                continue
            m = _PAGENUM_RE.match(ln)
            # Solo es «número de página» si es el de ESTA página y está en la cabecera/pie; una línea numérica suelta
            # en medio del texto (numerador/denominador de una fracción, celda de tabla) es contenido y se conserva.
            if m and i in edge and (re.search(r"[A-Za-z]", ln) or int(re.search(r"\d+", ln).group()) in (k, k + 1, k + 2)):
                continue
            kept.append(ln)
        out.append("\n".join(kept).strip("\n"))
    return out


def _numbers(text: str) -> set[str]:
    return {t.replace(",", ".") for t in _NUM_RE.findall(text)}


def extract_pages(path: str | Path, pdf_hash: str, cfg: dict, vision, progress, cancel: threading.Event,
                  math_vision: bool = True, phase=None, double_vision: bool = True):
    """Devuelve (lista de PageData, título, nº de páginas).

    - Texto con PyMuPDF; si falta, pdfplumber; si sigue faltando, visión.
    - Páginas con fórmulas (detección heurística) se transcriben con visión en LaTeX; si la visión
      pierde cifras respecto al texto vectorial, se añade ese texto como complemento.
    - progress(i, total, mensaje) se llama por página; phase('extract'|'vision') indica la fase actual.
    """
    ecfg = cfg["extraction"]
    min_chars = int(ecfg["min_chars_per_page"])
    doc = open_pdf(path)
    total = doc.page_count
    raw: list[str] = []
    if phase:
        phase("extract")
    for i in range(total):
        if cancel.is_set():
            raise Cancelled()
        progress(i, total, f"Extrayendo texto... página {i + 1}/{total}")
        try:
            raw.append(_clean(layout_text(doc[i])))      # texto en orden de lectura con las tablas reconstruidas
        except Exception as exc:  # noqa: BLE001
            log.warning("Error leyendo página %d: %s", i + 1, exc)
            raw.append("")

    if ecfg.get("remove_headers_footers", True):
        stripped = _strip_headers_footers(raw)
        # Solo se aplica el recorte si no vacía el documento
        if sum(len(t) for t in stripped) > 0.5 * sum(len(t) for t in raw):
            raw = stripped

    pages: list[PageData] = []
    pl_pdf = None
    n_math = 0
    threshold = int(ecfg.get("math_min_score", 5))
    crosscheck = float(ecfg.get("numeric_crosscheck", 0.85))
    for i, text in enumerate(raw):
        if cancel.is_set():
            raise Cancelled()
        source, original, eqs, vis_full = "texto", text, [], ""
        eq_flags, warns = [], []
        if len(text) < min_chars:
            # 1) pdfplumber (a veces extrae lo que PyMuPDF no)
            try:
                import pdfplumber
                if pl_pdf is None:
                    pl_pdf = pdfplumber.open(str(path))
                alt = _clean(pl_pdf.pages[i].extract_text() or "")
                if len(alt) >= min_chars:
                    text, source = alt, "pdfplumber"
            except Exception as exc:  # noqa: BLE001
                log.info("pdfplumber no pudo leer la página %d: %s", i + 1, exc)

        scanned = len(text) < min_chars
        has_math = math_vision and not scanned and math_score(text) >= threshold
        if scanned or has_math:
            reason = "OCR (poco texto)" if scanned else "fórmulas detectadas"
            if phase:
                phase("vision")
            progress(i, total, f"Visión: página {i + 1}/{total} · {reason}")
            log.info("Página %d: transcripción con %s (%s)", i + 1, cfg["ollama"]["vision_model"], reason)
            try:
                img = render_page(doc, i, int(ecfg["ocr_dpi"]), pdf_hash)
                vis = _clean_vision(vision.transcribe_page(img, i + 1))
                vis2 = ""
                if double_vision:
                    # Segunda lectura a otra resolución: si las dos no coinciden, el dato queda marcado como dudoso
                    img2 = render_page(doc, i, int(ecfg.get("verify_dpi", 300)), pdf_hash)
                    vis2 = _clean_vision(vision.transcribe_page(img2, i + 1))
                if scanned:
                    # Página escaneada: la visión es la ÚNICA fuente de texto
                    if len(vis) > len(text):
                        text, source = vis, "visión"
                        if vis2:
                            bad = diff_lines(vis, vis2)
                            if bad:
                                warns.append(f"OCR: {len(bad)} línea(s) difieren entre las dos lecturas (p. ej. «{bad[0][:60]}»)")
                                text = "\n".join(("⚠ " + ln) if ln.strip() in {b.strip() for b in bad} else ln
                                                 for ln in text.splitlines())
                else:
                    # Página con fórmulas: el texto vectorial sigue siendo la fuente principal (palabras, cifras y
                    # títulos exactos). La visión solo aporta las fórmulas en LaTeX, que PyMuPDF desordena
                    # (fracciones, límites de integrales...). Comprobado: la visión puede inventar encabezados.
                    eqs = extract_equations(vis)
                    if eqs:
                        source = "texto+fórmulas"
                        vis_full = vis
                        n_math += 1
                        eqs2 = extract_equations(vis2) if vis2 else None
                        eq_flags = [{"ok": c.ok, "why": c.why, "alt": c.alt} for c in compare_readings(eqs, eqs2, original)]
                        warns += operator_audit([e for _, e in eqs], original)
                        if ecfg.get("third_reading", False) and mathocr.available():
                            # Tercera lectura opcional con pix2tex (CPU): contrasta los símbolos de cada fórmula
                            for k, why in mathocr.third_reading(doc[i], [e for _, e in eqs]).items():
                                if k < len(eq_flags):
                                    eq_flags[k]["ok"] = False
                                    eq_flags[k]["why"].append(why)
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("La visión falló en la página %d (se usa el texto extraído): %s", i + 1, exc)
            if phase:
                phase("extract")
        geo = []
        if has_math and not scanned:
            # Fase 0: la misma fórmula se reconstruye desde los glifos del PDF (sin modelo) para comprobarla y colocarla
            try:
                geo = [asdict(g) for g in mathgeom.page_formulas(doc[i])]
            except Exception as exc:  # noqa: BLE001
                log.warning("Reconstrucción geométrica de la página %d omitida: %s", i + 1, exc)
        if not text.strip():
            source = "vacía"
        pages.append(PageData(i + 1, text, source, original, eqs, vis_full, eq_flags, warns, geo))

    if pl_pdf is not None:
        pl_pdf.close()
    log.info("Extracción: %d páginas, %d leídas con visión (fórmulas u OCR)", total,
             sum(p.source in ("visión", "texto+fórmulas") for p in pages))

    title = (doc.metadata or {}).get("title") or ""
    if len(title.strip()) < 4:
        first = next((p.raw or p.text for p in pages if p.text), "")
        line = next((ln.strip() for ln in first.splitlines() if len(ln.strip()) > 4), "")
        title = line[:120]
    if not title:
        title = Path(path).stem
    doc.close()
    return pages, title.strip(), total
