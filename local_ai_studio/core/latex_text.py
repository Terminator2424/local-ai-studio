"""Conversión de fórmulas LaTeX a texto Unicode legible (para pantalla, PDF y Word cuando no hay imagen).

Vive en `core` porque la usan el visor, la exportación a PDF/Word y la interfaz; `pdf` depende de `core`, nunca al revés.
"""
from __future__ import annotations

import re

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
_SUB = dict(zip("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ", strict=True))
_SUP = dict(zip("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ", strict=True))
_WRAPPERS = ("text", "mathrm", "mathbf", "mathit", "operatorname", "textbf", "mathcal", "boldsymbol", "mbox")


_COMMAND_RE = re.compile(r"\\([A-Za-z]+|.)", re.S)      # «\frac», «\{», «\\»…


def _group(s: str, i: int):
    """Devuelve (contenido, índice tras el grupo) del bloque {..} que empieza en s[i]; si no hay llave, un solo carácter."""
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return "", i
    if s[i] == "\\":
        m = _COMMAND_RE.match(s, i)
        if m is None:                       # barra invertida al final (fórmula truncada): se trata como un carácter suelto
            return "\\", i + 1
        return m.group(0), m.end()
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
            m = _COMMAND_RE.match(s, i)
            if m is None:                   # barra invertida al final (fórmula truncada): se conserva tal cual
                out.append("\\")
                i += 1
                continue
            name = "\\" + m.group(1)
            j = m.end()
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
ARRAY_RE = re.compile(r"\\begin\{" + _ENV + r"\}(?:\{[^}]*\})?(.*?)\\end\{" + _ENV + r"\}", re.S)


def array_to_table(body: str) -> str:
    """Convierte un entorno array/tabular/matrix de LaTeX en una tabla Markdown."""
    m = ARRAY_RE.search(body)
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


MATH_SPAN = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)|(?<![\\$])\$([^$\n]+?)\$", re.S)


def latex_to_text(md: str) -> str:
    """Convierte las fórmulas LaTeX de un Markdown a texto Unicode legible (para pantalla y PDF)."""
    def rep(m):
        body = next(g for g in m.groups() if g is not None)
        if ARRAY_RE.search(body):
            return "\n\n" + array_to_table(body) + "\n\n"
        txt = re.sub(r"\s+", " ", _convert(body)).strip()
        return f"\n\n    {txt}\n\n" if (m.group(1) is not None or m.group(2) is not None) else txt
    return MATH_SPAN.sub(rep, md)
