"""Procesado de texto: tildes sueltas, limpieza de salidas del modelo, LaTeX legible y auditoría numérica."""
from __future__ import annotations

import re

# ------------------------------------------------------------ tildes sueltas
# Los PDFs de LaTeX guardan «á» como «´» + «a»; PyMuPDF los extrae separados («Integraci´on»).
_ACC = {
    "´": dict(zip("aeiouAEIOUı", "áéíóúÁÉÍÓÚí", strict=True)),
    "¨": {"u": "ü", "U": "Ü", "a": "ä", "e": "ë", "i": "ï", "o": "ö", "A": "Ä", "E": "Ë", "I": "Ï", "O": "Ö"},
    "˜": {"n": "ñ", "N": "Ñ", "a": "ã", "o": "õ"},
    "`": dict(zip("aeiouAEIOU", "àèìòùÀÈÌÒÙ", strict=True)),
}
_ACC_RE = re.compile("([´¨˜`])([A-Za-zı])")


def fix_accents(text: str) -> str:
    def rep(m):
        return _ACC.get(m.group(1), {}).get(m.group(2), m.group(0))
    return _ACC_RE.sub(rep, text)


# ----------------------------------------------- limpieza de respuestas del LLM
_FENCE_RE = re.compile(r"```[ \t]*(?:markdown|md|latex|tex|text)?[ \t]*\n(.*?)```", re.S | re.I)
_META_RE = re.compile(
    r"^(este|esta|aquí|a continuación|el siguiente|the following|this|here is|here's)\b[^\n]{0,200}"
    r"(mejora|mejorad|crítica|critica|omisiones|corrig|incorpor|improv|critique|omission|revised|detalla los pasos)",
    re.I)


_UNWRAP_LANGS = {"markdown", "md", "latex", "tex", "text", "txt"}


def _unwrap_fences(text: str) -> str:
    """Quita las vallas ```markdown/latex/text que envuelven la respuesta, pero CONSERVA los bloques de código
    (```javascript, ```python, ```), que son contenido del documento."""
    out: list[str] = []
    in_fence, unwrap = False, False
    for ln in text.splitlines():
        m = re.match(r"^\s*```\s*([A-Za-z0-9_+#.-]*)\s*$", ln)
        if m:
            if not in_fence:
                in_fence, unwrap = True, m.group(1).lower() in _UNWRAP_LANGS
                if unwrap:
                    continue
            else:
                in_fence = False
                if unwrap:
                    unwrap = False
                    continue
        out.append(ln)
    if in_fence and not unwrap:        # bloque de código sin cerrar: se cierra
        out.append("```")
    return "\n".join(out)


def clean_output(text: str) -> str:
    """Quita vallas de envoltorio, la palabra suelta «markdown» y comentarios del modelo sobre su propio trabajo.
    Los bloques de código del documento se conservan."""
    import unicodedata
    text = unicodedata.normalize("NFC", text)       # tildes bien compuestas
    text = _unwrap_fences(text)
    paragraphs = re.split(r"\n{2,}", text.strip())
    kept = []
    for p in paragraphs:
        s = p.strip()
        if s.lower() in ("markdown", "md"):
            continue
        s = re.sub(r"^(markdown|md)\n", "", s, flags=re.I)
        if len(s) < 450 and _META_RE.match(s):
            continue
        kept.append(s)
    return "\n\n".join(kept).strip()


# --------------------------------------------------------- auditoría numérica
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
_CITE_RE = re.compile(r"\((?:p|pp|pág|págs|pags|page|pages)\.?[^)]*\)|\b(?:p|pp|pág|págs|page|pages)\.\s*\d+(?:\s*[-–]\s*\d+)?", re.I)


def number_tokens(text: str) -> set[str]:
    return {t.replace(",", ".") for t in _NUM_RE.findall(text)}


def _significant(tok: str) -> bool:
    return "." in tok or len(tok) >= 2


def audit_numbers(body: str, full_md: str, source: str) -> dict:
    """Mide qué porcentaje de las cifras de la fuente aparece en el resumen y qué cifras del resumen no están en la fuente."""
    src = {t for t in number_tokens(source) if _significant(t)}
    # Ignora citas de página, encabezados de parte y la línea de metadatos del resumen
    clean_body = _CITE_RE.sub("", body)
    clean_body = re.sub(r"^#{1,4}\s*(Parte|Part)\s+\d+.*$", "", clean_body, flags=re.M)
    clean_body = "\n".join(ln for ln in clean_body.splitlines() if not ln.lstrip().startswith(">"))
    in_summary = {t for t in number_tokens(clean_body) if _significant(t)}
    in_full = {t for t in number_tokens(_CITE_RE.sub("", full_md)) if _significant(t)}
    coverage = len(src & in_full) / len(src) if src else 1.0
    unsupported = sorted(in_summary - src, key=lambda x: (len(x), x))
    return {"coverage": round(coverage * 100, 1), "unsupported": unsupported, "source_numbers": len(src)}


def unsupported_numbers(text: str, source: str) -> list[str]:
    """Cifras significativas de 'text' que no aparecen en 'source' (ignora citas de página y encabezados)."""
    src = number_tokens(source)
    clean = _CITE_RE.sub("", text)
    clean = re.sub(r"^#{1,4}\s.*$", "", clean, flags=re.M)
    found = {t for t in number_tokens(clean) if _significant(t)}
    return sorted(found - src, key=lambda x: (len(x), x))


# ------------------------------------------------- ecuaciones de la visión
_EQ_RE = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|(?<![\\$])\$([^$\n]{3,}?)\$", re.S)


def extract_equations(text: str) -> list[list]:
    """Fórmulas LaTeX de una transcripción de visión: [[posición_relativa, latex], ...] en orden de aparición."""
    n = len(text) or 1
    out: list[list] = []
    for m in _EQ_RE.finditer(text):
        body = re.sub(r"\s+", " ", next(g for g in m.groups() if g is not None)).strip()
        if len(body) < 3 or "\\begin{array}" in body or "\\begin{tabular}" in body:
            continue
        if m.group(3) is not None and not re.search(r"\\[A-Za-z]+|[_^=]", body):
            continue                      # $x$ suelto: no es una fórmula
        if out and out[-1][1] == body:
            continue
        out.append([round(m.start() / n, 4), body])
    return out


# --------------------------------------------- términos inventados / notas
_CAP_RE = re.compile(r"(?<=[a-záéíóúñ,;:(] )([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{4,})")


def _fold(word: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", word.lower()) if unicodedata.category(c) != "Mn")


def unknown_terms(text: str, source: str) -> list[str]:
    """Palabras con mayúscula inicial a mitad de frase (nombres, disciplinas...) que no aparecen en la fuente."""
    src = {_fold(w) for w in re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{4,}", source)}
    clean = re.sub(r"\$\$.*?\$\$|\$[^$\n]*\$|\\\(.*?\\\)|\\\[.*?\\\]", " ", text, flags=re.S)
    clean = _CITE_RE.sub("", clean)
    out: list[str] = []
    for m in _CAP_RE.finditer(clean):
        w = m.group(1)
        if _fold(w) not in src and w not in out:
            out.append(w)
    return out


_EMPTY_RE = re.compile(
    r"^(?:[-*•]\s*)?(?:—|-|–|n/?a|ninguno|ninguna|none|not applicable"
    r"|no (?:hay|se (?:presenta|proporciona|muestra|mencionan?|incluye|indica|describe)|aplica|existen?|contiene)\b[^\n]*"
    r"|there (?:is|are) no\b[^\n]*|no [a-z ]{0,30}(?:provided|mentioned|shown|included)\b[^\n]*)\.?\s*$", re.I)


def section_text(note: str, names: tuple[str, ...]) -> str:
    """Devuelve el contenido de las secciones cuyo título empieza por alguno de 'names' (para el resumen ejecutivo)."""
    parts = re.split(r"^#{3,4}\s+(.+?)\s*$", note, flags=re.M)
    out = []
    for i in range(1, len(parts), 2):
        if parts[i].strip(" *").lower().startswith(tuple(n.lower() for n in names)):
            body = parts[i + 1].strip()
            lines = [ln for ln in body.splitlines() if ln.strip()]
            if lines and not all(_EMPTY_RE.match(ln.strip()) for ln in lines):
                out.append(body)
    return "\n".join(out)


# ------------------------------------------------------ compactación (tokens)
def compact_latex(md: str) -> str:
    """Reduce tokens sin cambiar el significado: quita adornos LaTeX, unifica delimitadores y compacta tablas."""
    md = re.sub(r"\\\(\s*(.+?)\s*\\\)", lambda m: f"${m.group(1)}$", md, flags=re.S)
    md = re.sub(r"\\\[\s*(.+?)\s*\\\]", lambda m: f"$$ {m.group(1)} $$", md, flags=re.S)
    md = re.sub(r"\\(?:left|right|displaystyle)(?![A-Za-z])\s?", "", md)
    md = re.sub(r"\\[,;!:]", "", md)
    md = re.sub(r"\\q?quad", " ", md)
    md = md.replace("{}^\\circ", "°").replace("^\\circ", "°").replace("^{\\circ}", "°")
    out: list[str] = []
    prev_cols = 0
    for ln in md.splitlines():
        ln = re.sub(r"^\s*[-*]\s+(?=\|)", "", ln)             # «- | a | b |» → «| a | b |» (tabla real)
        if ln.lstrip().startswith("|") and out and out[-1].strip() and not out[-1].lstrip().startswith("|"):
            out.append("")                                       # Markdown exige línea en blanco antes de la tabla
        if ln.lstrip().startswith("|"):
            ln = re.sub(r"[ \t]{2,}", " ", ln.strip())
            if re.match(r"^\|(?:\s*:?-+:?\s*\|)+\s*$", ln):
                ln = "|" + "---|" * (prev_cols or ln.count("|") - 1)     # tantas columnas como el encabezado
            else:
                prev_cols = max(1, ln.count("|") - 1)
        else:
            ln = re.sub(r"[ \t]{2,}(?=\S)", lambda m: m.group(0) if m.start() == 0 else " ", ln) if not ln.startswith(" ") else ln
        out.append(ln.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))
