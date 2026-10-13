"""Procesado de texto: tildes sueltas, limpieza de salidas del modelo, LaTeX legible y auditoría numérica."""
from __future__ import annotations

import re

# ------------------------------------------------------------ tildes sueltas
# Los PDFs de LaTeX guardan «á» como «´» + «a»; PyMuPDF los extrae separados («Integraci´on»).
_ACC = {
    "´": dict(zip("aeiouAEIOUı", "áéíóúÁÉÍÓÚí")),
    "¨": {"u": "ü", "U": "Ü", "a": "ä", "e": "ë", "i": "ï", "o": "ö", "A": "Ä", "E": "Ë", "I": "Ï", "O": "Ö"},
    "˜": {"n": "ñ", "N": "Ñ", "a": "ã", "o": "õ"},
    "`": dict(zip("aeiouAEIOU", "àèìòùÀÈÌÒÙ")),
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


# ------------------------------------------------------------ LaTeX -> texto
_SYMS = {
    r"\iiint": "∭", r"\iint": "∬", r"\int": "∫", r"\sum": "∑", r"\prod": "∏", r"\approx": "≈", r"\cdot": "·",
    r"\times": "×", r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥", r"\neq": "≠", r"\ne": "≠",
    r"\pm": "±", r"\infty": "∞", r"\to": "→", r"\rightarrow": "→", r"\Rightarrow": "⇒", r"\leftarrow": "←",
    r"\circ": "°", r"\partial": "∂", r"\in": "∈", r"\ldots": "…", r"\cdots": "⋯", r"\dots": "…",
    r"\Delta": "Δ", r"\delta": "δ", r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\theta": "θ",
    r"\lambda": "λ", r"\mu": "μ", r"\pi": "π", r"\sigma": "σ", r"\omega": "ω", r"\Omega": "Ω", r"\epsilon": "ε",
    r"\rho": "ρ", r"\phi": "φ", r"\tau": "τ", r"\nabla": "∇", r"\forall": "∀", r"\exists": "∃",
    r"\mathbb{R}": "ℝ", r"\sim": "∼", r"\equiv": "≡", r"\propto": "∝", r"\div": "÷",
}
_SUB = dict(zip("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))
_SUP = dict(zip("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ"))
_WRAPPERS = ("text", "mathrm", "mathbf", "mathit", "operatorname", "textbf", "mathcal", "boldsymbol", "mbox")


def _group(s: str, i: int):
    """Devuelve (contenido, índice tras el grupo) del bloque {..} que empieza en s[i]; si no hay llave, un solo carácter."""
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return "", i
    if s[i] == "\\":
        m = re.match(r"\\([A-Za-z]+|.)", s[i:])
        return m.group(0), i + len(m.group(0))
    if s[i] != "{":
        return s[i], i + 1
    depth, j = 0, i
    while j < len(s):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return s[i + 1:], len(s)


def _script(content: str, table: dict, marker: str) -> str:
    content = content.strip()
    if content == "°":
        return "°"
    if content and all(c in table for c in content):
        return "".join(table[c] for c in content)
    return f"{marker}({content})" if len(content) > 1 else f"{marker}{content}"


def _simple(s: str) -> bool:
    return bool(re.fullmatch(r"[\w.,°]+", s.strip()))


def _convert(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+|.)", s[i:])
            name = "\\" + m.group(1)
            j = i + len(m.group(0))
            if m.group(1) in ("frac", "dfrac", "tfrac"):
                a, j = _group(s, j)
                b, j = _group(s, j)
                a, b = _convert(a).strip(), _convert(b).strip()
                out.append(f"{a if _simple(a) else '(' + a + ')'}/{b if _simple(b) else '(' + b + ')'}")
            elif m.group(1) == "sqrt":
                a, j = _group(s, j)
                out.append(f"√({_convert(a)})")
            elif m.group(1) in _WRAPPERS:
                a, j = _group(s, j)
                out.append(_convert(a))
            elif m.group(1) in ("left", "right", "displaystyle", "big", "Big", "bigg", "limits", "nolimits"):
                if j < len(s) and s[j] in "([{|.)]}" and m.group(1) in ("left", "right"):
                    out.append("" if s[j] == "." else s[j])
                    j += 1
            elif m.group(1) in (",", ";", ":", "!", "quad", "qquad", " "):
                out.append(" ")
            elif name in _SYMS:
                out.append(_SYMS[name])
            elif m.group(1) in ("{", "}", "%", "$", "&", "_", "#"):
                out.append(m.group(1))
            elif m.group(1) == "\\":
                out.append(" ")
            else:
                out.append(m.group(1))  # comando desconocido: deja el nombre
            i = j
        elif c in "_^":
            g, j = _group(s, i + 1)
            g = _convert(g)
            out.append(_script(g, _SUB, "_") if c == "_" else _script(g, _SUP, "^"))
            i = j
        elif c in "{}":
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


_ENV = r"(?:array|tabular|[pbvBV]?matrix)"
_ARRAY_RE = re.compile(r"\\begin\{" + _ENV + r"\}(?:\{[^}]*\})?(.*?)\\end\{" + _ENV + r"\}", re.S)


def _array_to_table(body: str) -> str:
    """Convierte un entorno array/tabular/matrix de LaTeX en una tabla Markdown."""
    m = _ARRAY_RE.search(body)
    inner = m.group(1) if m else body
    inner = re.sub(r"\\hline|\\cline\{[^}]*\}", "", inner)
    rows = [r.strip() for r in re.split(r"\\\\", inner) if r.strip()]
    cells = [[_convert(c).strip() for c in r.split("&")] for r in rows]
    if not cells:
        return ""
    ncol = max(len(r) for r in cells)
    cells = [r + [""] * (ncol - len(r)) for r in cells]
    lines = ["| " + " | ".join(cells[0]) + " |", "|" + "---|" * ncol]
    lines += ["| " + " | ".join(r) + " |" for r in cells[1:]]
    return "\n".join(lines)


_MATH_SPAN = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)|(?<![\\$])\$([^$\n]+?)\$", re.S)


def latex_to_text(md: str) -> str:
    """Convierte las fórmulas LaTeX de un Markdown a texto Unicode legible (para pantalla y PDF)."""
    def rep(m):
        body = next(g for g in m.groups() if g is not None)
        if _ARRAY_RE.search(body):
            return "\n\n" + _array_to_table(body) + "\n\n"
        txt = re.sub(r"\s+", " ", _convert(body)).strip()
        return f"\n\n    {txt}\n\n" if (m.group(1) is not None or m.group(2) is not None) else txt
    return _MATH_SPAN.sub(rep, md)


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


def compact_note(note: str) -> str:
    """Convierte las secciones «### X» de una nota en etiquetas en negrita y elimina las vacías («—», «No hay…»)."""
    parts = re.split(r"^#{3,4}\s+(.+?)\s*$", note, flags=re.M)
    if len(parts) < 3:
        return note.strip()
    out = []
    for i in range(1, len(parts), 2):
        label, body = parts[i].strip(" *"), parts[i + 1].strip()
        lines = [ln for ln in body.splitlines() if ln.strip()]
        if not lines or all(_EMPTY_RE.match(ln.strip()) for ln in lines):
            continue
        out.append(f"**{label}**\n\n{body}")
    return "\n\n".join(out) if out else "—"


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
    out = []
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
