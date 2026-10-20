"""Extracción: PyMuPDF -> pdfplumber -> visión (OCR y páginas con fórmulas) e imágenes de páginas."""
from __future__ import annotations

import contextlib
import logging
import re
import threading
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from ..core.errors import Cancelled, PDFError
from ..core.paths import IMAGES_DIR
from . import mathgeom, mathocr
from .layout import layout_text
from .textproc import extract_equations, fix_accents
from .verify import compare_readings, diff_lines, operator_audit

log = logging.getLogger(__name__)

_PAGENUM_RE = re.compile(r"^\s*(p[áa]g(ina)?\.?|page)?\s*\d+(\s*(de|of|/)\s*\d+)?\s*$", re.I)
_MATH_CHARS = set("∫∑∏≈≤≥≠±√∞∂∈→−×÷∇∮")
_ORPHAN_RE = re.compile(r"^(Z{1,3}|[0-9]{1,2}|[A-Za-z]{1,2})$")

# --- SEGURIDAD: Prevención de Path Traversal ---
def _is_safe_path(path: str | Path, base_dir: str | Path) -> bool:
    try:
        path_obj = Path(path).resolve()
        base_dir_obj = Path(base_dir).resolve()
        return base_dir_obj in path_obj.parents or path_obj == base_dir_obj
    except Exception:
        return False

def _validate_and_get_safe_path(path: str | Path, base_dir: str | Path | None = None) -> Path:
    if base_dir is None:
        base_dir = Path.home() # Permite archivos en Descargas, Escritorio, etc.
    if not _is_safe_path(path, base_dir):
        raise ValueError(f"Acceso denegado: la ruta '{path}' no está dentro del directorio permitido '{base_dir}'")
    return Path(path).resolve()
# -----------------------------------------------

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
    except Exception as exc:
        raise PDFError(f"No se pudo abrir el PDF (¿corrupto?): {exc}") from exc
    if doc.needs_pass and not doc.authenticate(""):
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
            digits = re.search(r"\d+", ln)
            # Solo es «número de página» si es el de ESTA página y está en la cabecera/pie; una línea numérica suelta
            # en medio del texto (numerador/denominador de una fracción, celda de tabla) es contenido y se conserva.
            if (_PAGENUM_RE.match(ln) and digits and i in edge
                    and (re.search(r"[A-Za-z]", ln) or int(digits.group()) in (k, k + 1, k + 2))):
                continue
            kept.append(ln)
        out.append("\n".join(kept).strip("\n"))
    return out


@dataclass
class _PageState:
    """Lo que se va sabiendo de una página mientras se lee (texto final, procedencia, fórmulas, avisos)."""
    text: str
    source: str = "texto"
    eqs: list = field(default_factory=list)
    vis_full: str = ""
    eq_flags: list = field(default_factory=list)
    warns: list = field(default_factory=list)


class _LazyPlumber:
    """pdfplumber solo se abre si alguna página lo necesita, y se cierra siempre."""

    def __init__(self, path: str | Path):
        self._path = str(path)
        self._pdf: Any = None

    def text(self, index: int) -> str:
        import pdfplumber
        if self._pdf is None:
            self._pdf = pdfplumber.open(self._path)
        return _clean(self._pdf.pages[index].extract_text() or "")

    def close(self) -> None:
        if self._pdf is not None:
            self._pdf.close()
            self._pdf = None


def _read_all_text(doc: pymupdf.Document, ecfg: dict, progress, cancel: threading.Event, phase) -> list[str]:
    """Texto vectorial de todas las páginas (orden de lectura, tablas reconstruidas, sin encabezados repetidos)."""
    total = doc.page_count
    raw: list[str] = []
    if phase:
        phase("extract")
    for i in range(total):
        if cancel.is_set():
            raise Cancelled()
        progress(i, total, f"Extrayendo texto... página {i + 1}/{total}")
        try:
            raw.append(_clean(layout_text(doc[i])))
        except Exception as exc:
            log.warning("Error leyendo página %d: %s", i + 1, exc)
            raw.append("")
    if ecfg.get("remove_headers_footers", True):
        stripped = _strip_headers_footers(raw)
        # Solo se aplica el recorte si no vacía el documento
        if sum(len(t) for t in stripped) > 0.5 * sum(len(t) for t in raw):
            raw = stripped
    return raw


class _VisionReader:
    """Lee páginas con el modelo de visión: OCR si están escaneadas; fórmulas en LaTeX (dos lecturas) si no."""

    def __init__(self, doc: pymupdf.Document, pdf_hash: str, cfg: dict, vision, progress, phase, double_vision: bool):
        self.doc = doc
        self.pdf_hash = pdf_hash
        self.cfg = cfg
        self.ecfg = cfg["extraction"]
        self.vision = vision
        self.progress = progress
        self.phase = phase
        self.double_vision = double_vision

    def read(self, i: int, total: int, st: _PageState, original: str, scanned: bool) -> None:
        reason = "OCR (poco texto)" if scanned else "fórmulas detectadas"
        if self.phase:
            self.phase("vision")
        self.progress(i, total, f"Visión: página {i + 1}/{total} · {reason}")
        log.info("Página %d: transcripción con %s (%s)", i + 1, self.cfg["ollama"]["vision_model"], reason)
        try:
            img = render_page(self.doc, i, int(self.ecfg["ocr_dpi"]), self.pdf_hash)
            vis = _clean_vision(self.vision.transcribe_page(img, i + 1))
            vis2 = ""
            if self.double_vision:
                # Segunda lectura a otra resolución: si las dos no coinciden, el dato queda marcado como dudoso
                img2 = render_page(self.doc, i, int(self.ecfg.get("verify_dpi", 300)), self.pdf_hash)
                vis2 = _clean_vision(self.vision.transcribe_page(img2, i + 1))
            if scanned:
                self._scanned(st, vis, vis2)
            else:
                self._formulas(st, i, vis, vis2, original)
        except Cancelled:
            raise
        except Exception as exc:
            log.warning("La visión falló en la página %d (se usa el texto extraído): %s", i + 1, exc)
        if self.phase:
            self.phase("extract")

    @staticmethod
    def _scanned(st: _PageState, vis: str, vis2: str) -> None:
        """Página escaneada: la visión es la ÚNICA fuente de texto."""
        if len(vis) <= len(st.text):
            return
        st.text, st.source = vis, "visión"
        bad = diff_lines(vis, vis2) if vis2 else []
        if bad:
            st.warns.append(f"OCR: {len(bad)} línea(s) difieren entre las dos lecturas (p. ej. «{bad[0][:60]}»)")
            doubtful = {b.strip() for b in bad}
            st.text = "\n".join(("⚠ " + ln) if ln.strip() in doubtful else ln for ln in st.text.splitlines())

    def _formulas(self, st: _PageState, i: int, vis: str, vis2: str, original: str) -> None:
        """Página con fórmulas: el texto vectorial sigue siendo la fuente principal (palabras, cifras y títulos exactos).
        La visión solo aporta las fórmulas en LaTeX, que PyMuPDF desordena (fracciones, límites de integrales...).
        Comprobado: la visión puede inventar encabezados."""
        st.eqs = extract_equations(vis)
        if not st.eqs:
            return
        st.source = "texto+fórmulas"
        st.vis_full = vis
        eqs2 = extract_equations(vis2) if vis2 else None
        st.eq_flags = [{"ok": c.ok, "why": c.why, "alt": c.alt} for c in compare_readings(st.eqs, eqs2, original)]
        latex = [e for _, e in st.eqs]
        st.warns += operator_audit(latex, original)
        if self.ecfg.get("third_reading", False) and mathocr.available():
            # Tercera lectura opcional con pix2tex (CPU): contrasta los símbolos de cada fórmula
            for k, why in mathocr.third_reading(self.doc[i], latex).items():
                if k < len(st.eq_flags):
                    st.eq_flags[k]["ok"] = False
                    st.eq_flags[k]["why"].append(why)


def _document_title(doc: pymupdf.Document, pages: list[PageData], path: str | Path) -> str:
    title = (doc.metadata or {}).get("title") or ""
    if len(title.strip()) < 4:
        first = next((p.raw or p.text for p in pages if p.text), "")
        title = next((ln.strip() for ln in first.splitlines() if len(ln.strip()) > 4), "")[:120]
    return (title or Path(path).stem).strip()


def extract_pages(path: str | Path, pdf_hash: str, cfg: dict, vision, progress, cancel: threading.Event,
                  math_vision: bool = True, phase=None, double_vision: bool = True):
    """Devuelve (lista de PageData, título, nº de páginas)."""

    # VALIDACIÓN DE SEGURIDAD
    path = _validate_and_get_safe_path(path)

    ecfg = cfg["extraction"]
    min_chars = int(ecfg["min_chars_per_page"])
    threshold = int(ecfg.get("math_min_score", 5))
    plumber = _LazyPlumber(path)
    with contextlib.closing(open_pdf(path)) as doc:
        try:
            total = doc.page_count
            raw = _read_all_text(doc, ecfg, progress, cancel, phase)
            reader = _VisionReader(doc, pdf_hash, cfg, vision, progress, phase, double_vision)
            pages: list[PageData] = []
            for i, original in enumerate(raw):
                if cancel.is_set():
                    raise Cancelled()
                st = _PageState(original)
                if len(st.text) < min_chars:
                    try:
                        alt = plumber.text(i)
                        if len(alt) >= min_chars:
                            st.text, st.source = alt, "pdfplumber"
                    except Exception as exc:
                        log.info("pdfplumber no pudo leer la página %d: %s", i + 1, exc)
                scanned = len(st.text) < min_chars
                has_math = math_vision and not scanned and math_score(st.text) >= threshold
                if scanned or has_math:
                    reader.read(i, total, st, original, scanned)
                geo: list = []
                if has_math:
                    try:
                        geo = [asdict(g) for g in mathgeom.page_formulas(doc[i])]
                    except Exception as exc:
                        log.warning("Reconstrucción geométrica de la página %d omitida: %s", i + 1, exc)
                if not st.text.strip():
                    st.source = "vacía"
                pages.append(PageData(i + 1, st.text, st.source, original, st.eqs, st.vis_full, st.eq_flags, st.warns, geo))
        finally:
            plumber.close()
        log.info("Extracción: %d páginas, %d leídas con visión (fórmulas u OCR)", total,
                 sum(p.source in ("visión", "texto+fórmulas") for p in pages))
        title = _document_title(doc, pages, path)
    return pages, title, total
