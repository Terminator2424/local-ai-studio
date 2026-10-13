"""Fase 0: reconstruir las fórmulas de un PDF de texto (LaTeX) desde sus propios glifos, sin modelo.

Un PDF generado con LaTeX guarda cada símbolo con su fuente (CMMI10, CMR10, CMSY10, CMEX10…), su tamaño y su línea
base, y las fracciones son reglas horizontales. Con eso se reconstruye el LaTeX de cada bloque:
- fracción: regla horizontal; lo que queda encima (centro) es el numerador y lo de debajo el denominador;
- subíndice/superíndice: glifo más pequeño que el texto principal, desplazado abajo/arriba de su átomo;
- integral (CMEX10 «Z») y sumatorio («X»): sus límites son los glifos pequeños a la derecha (∫) o encima/debajo (∑).

Si aparece algo que no sabemos explicar (glifo sin mapeo, regla que no es fracción, fracción sin numerador o
denominador, texto dentro de la fórmula…), el bloque se marca como incompleto y la fórmula se queda con la lectura
de la visión, como antes. Así el modelo nunca decide dígitos ni estructura cuando el PDF los dice exactamente.
"""
from __future__ import annotations

import difflib
import re
import string
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

from .layout import _find_tables, _rows

# Fuentes matemáticas de TeX (y sus versiones de subíndice: CMR7, CMMI7, CMSY7…). Cualquier otra fuente no es fórmula:
# una palabra de 4+ letras sin fuente matemática es prosa o código, nunca una fórmula.
_MATH_FONT = re.compile(r"^(CMMI|CMSY|CMEX|MSAM|MSBM)|^CM[A-Z]{1,4}(5|7)$")
_BIG = {"Z": "\\int", "∫": "\\int", "X": "\\sum", "∑": "\\sum"}   # CMEX10: Z = integral, X = sumatorio
_SYMBOLS = {"−": "-", "·": "\\cdot ", "⋅": "\\cdot ", "×": "\\times ", "≈": "\\approx ", "≤": "\\leq ",
            "≥": "\\geq ", "≠": "\\neq ", "∈": "\\in ", "◦": "\\circ ", "°": "\\circ ", "{": "\\{ ", "}": "\\} ",
            "±": "\\pm ", "∞": "\\infty ", "→": "\\to ", "∂": "\\partial ", "∇": "\\nabla "}
_ASCII_OK = set(string.ascii_letters + string.digits + "()[]+=,./:;!|<>'\"*&")
_SUP_SHIFT = 1.8     # pt: un glifo pequeño más alto que esto sobre su átomo es superíndice; si no, subíndice
_SCRIPT_RATIO = 0.9  # tamaño relativo al texto principal por debajo del cual un glifo es subíndice/superíndice


class Unsupported(Exception):
    """El bloque tiene algo que no sabemos explicar: no se usa la reconstrucción."""


@dataclass
class Glyph:
    c: str
    font: str
    size: float
    x0: float
    y0: float
    x1: float
    y1: float
    base: float      # línea base (origen y del glifo, crece hacia abajo como en PyMuPDF)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Bar:
    x0: float
    x1: float
    y: float


@dataclass
class _Item:
    x0: float
    x1: float
    base: float
    piece: str
    script: bool
    roman: bool


@dataclass
class _Atom:
    item: _Item
    sub: list = field(default_factory=list)
    sup: list = field(default_factory=list)

    @property
    def base(self) -> float:
        return self.item.base

    @property
    def x1(self) -> float:
        return max([self.item.x1] + [s.x1 for s in self.sub + self.sup])

    def render(self) -> str:
        out = self.item.piece
        if self.sub:
            out += _script("_", self.sub)
        if self.sup:
            out += _script("^", self.sup)
        return out


def _pieces(items: list[_Item]) -> str:
    """Concatena trozos; las letras en fuente romana consecutivas van juntas en un \\mathrm{…}."""
    out, buf = [], []
    for it in items:
        if it.roman:
            buf.append(it.piece)
            continue
        if buf:
            out.append("\\mathrm{" + "".join(buf) + "}")
            buf = []
        out.append(it.piece)
    if buf:
        out.append("\\mathrm{" + "".join(buf) + "}")
    return "".join(out)


def _script(sym: str, items: list[_Item]) -> str:
    text = _pieces(sorted(items, key=lambda it: it.x0))
    if len(text) == 1 and text.isalnum():
        return sym + text
    return f"{sym}{{{text}}}"


# ----------------------------------------------------------------- reconstrucción
def _is_big(g: Glyph) -> bool:
    return g.c in _BIG and (g.font.startswith("CMEX") or g.c in "∫∑")


def _main_size(gs: list[Glyph]) -> float:
    """Tamaño del texto principal de una región: el mayor tamaño de los glifos que no son operadores grandes.
    Los subíndices, límites y exponentes son siempre más pequeños que el texto que los lleva, así que el máximo
    no se confunde con ellos (la moda sí: un bloque con muchos límites tiene más glifos pequeños que grandes)."""
    sizes = [round(g.size, 1) for g in gs if not _is_big(g)]
    return max(sizes) if sizes else max(g.size for g in gs)


def _is_script(g: Glyph, m: float) -> bool:
    return g.size < _SCRIPT_RATIO * m


def _sym(g: Glyph) -> str:
    if g.c in _SYMBOLS:
        return _SYMBOLS[g.c]
    if g.c in _ASCII_OK:
        return g.c
    raise Unsupported(f"glifo sin mapeo «{g.c}» ({g.font})")


def _top_bars(bars: list[Bar]) -> list[Bar]:
    """Reglas que no están dentro de otra regla: son fracciones de primer nivel."""
    tops = []
    for b in bars:
        containers = [c for c in bars if c is not b and c.x0 - 0.5 <= b.x0 and b.x1 <= c.x1 + 0.5]
        if any(abs(c.x0 - b.x0) < 0.5 and abs(c.x1 - b.x1) < 0.5 for c in containers):
            raise Unsupported("dos reglas de fracción con el mismo ancho")
        for c in bars:
            if c is b:
                continue
            overlap = b.x0 < c.x1 - 0.5 and c.x0 < b.x1 - 0.5
            nested = (c.x0 - 0.5 <= b.x0 and b.x1 <= c.x1 + 0.5) or (b.x0 - 0.5 <= c.x0 and c.x1 <= b.x1 + 0.5)
            if overlap and not nested:
                raise Unsupported("reglas que se solapan")
        if not containers:
            tops.append(b)
    return tops


def _fraction_items(gs: list[Glyph], bars: list[Bar], m: float, used: list[bool], items: list[_Item]) -> None:
    """1) Fracciones: reglas de primer nivel; lo de encima es el numerador y lo de debajo el denominador."""
    for b in sorted(_top_bars(bars), key=lambda r: r.x0):
        ids = [i for i, g in enumerate(gs) if not used[i] and b.x0 - 1.0 <= g.cx <= b.x1 + 1.0]
        num = [i for i in ids if gs[i].cy < b.y]
        den = [i for i in ids if gs[i].cy > b.y]
        if len(num) + len(den) != len(ids):
            raise Unsupported("glifo que cruza una regla de fracción")
        if not num or not den:
            raise Unsupported("fracción sin numerador o denominador")
        for i in ids:
            used[i] = True
        nb = [c for c in bars if c is not b and c.y < b.y and c.x0 >= b.x0 - 0.5 and c.x1 <= b.x1 + 0.5]
        db = [c for c in bars if c is not b and c.y > b.y and c.x0 >= b.x0 - 0.5 and c.x1 <= b.x1 + 0.5]
        n = _layout([gs[i] for i in num], nb)
        d = _layout([gs[i] for i in den], db)
        items.append(_Item(b.x0, b.x1, b.y + 0.25 * m, f"\\frac{{{n}}}{{{d}}}", False, False))


def _limits_of(g: Glyph, gs: list[Glyph], m: float, used: list[bool], bigs: list[Glyph]) -> tuple[list[Glyph], list[Glyph]]:
    """Límites (inferior, superior) de un operador grande; los marca como usados.
    ∫: límites a la derecha del signo; ∑: centrados encima y debajo, y solo del ∑ más cercano (dos ∑ seguidos)."""
    op = _BIG[g.c]
    lo: list[Glyph] = []
    up: list[Glyph] = []
    for j, s in enumerate(gs):
        if used[j] or _is_big(s) or not _is_script(s, m):
            continue
        if op == "\\int":
            near = g.x1 - 1.5 <= s.x0 <= g.x1 + 10.0
        else:
            nearest = min((b for b in bigs if _BIG[b.c] == op), key=lambda b: abs(s.cx - b.cx))
            near = nearest is g and abs(s.cx - g.cx) <= 14.0
        if not near:
            continue
        used[j] = True
        (up if s.cy < g.cy else lo).append(s)
    return lo, up


def _big_operator_items(gs: list[Glyph], m: float, used: list[bool], items: list[_Item]) -> None:
    """2) Operadores grandes (∫, ∑) con sus límites."""
    bigs = [g for g in gs if _is_big(g)]
    for i, g in enumerate(gs):
        if used[i] or not _is_big(g):
            continue
        used[i] = True
        lo, up = _limits_of(g, gs, m, used, bigs)
        latex = _BIG[g.c]
        if lo:
            latex += f"_{{{_layout(lo, [])}}}"
        if up:
            latex += f"^{{{_layout(up, [])}}}"
        items.append(_Item(g.x0, g.x1, g.base, latex, False, False))


def _plain_items(gs: list[Glyph], m: float, used: list[bool], items: list[_Item]) -> None:
    """3) Resto de glifos: átomos y subíndices/superíndices."""
    for i, g in enumerate(gs):
        if used[i]:
            continue
        if _is_big(g):
            raise Unsupported(f"operador «{g.c}» sin tratar")
        roman = g.font.startswith("CMR") and g.c.isalpha()     # letra en fuente romana: se escribe \mathrm{…}
        items.append(_Item(g.x0, g.x1, g.base, g.c if roman else _sym(g), _is_script(g, m), roman))


def _fold_items(items: list[_Item]) -> str:
    """Plegado en orden de x: un glifo pequeño se engancha al átomo anterior según su desplazamiento vertical."""
    items.sort(key=lambda it: it.x0)
    atoms: list[_Atom] = []
    for it in items:
        if it.script:
            if not atoms:
                raise Unsupported("subíndice sin base")
            rise = atoms[-1].base - it.base
            (atoms[-1].sup if rise >= _SUP_SHIFT else atoms[-1].sub).append(it)
        else:
            atoms.append(_Atom(it))
    return _join([(a.render(), a.item.x0, a.x1) for a in atoms])


def _layout(gs: list[Glyph], bars: list[Bar]) -> str:
    """LaTeX de una región: fracciones, operadores grandes, átomos con sub/superíndices."""
    if not gs:
        raise Unsupported("región vacía")
    m = _main_size(gs)
    used = [False] * len(gs)
    items: list[_Item] = []
    _fraction_items(gs, bars, m, used, items)
    _big_operator_items(gs, m, used, items)
    _plain_items(gs, m, used, items)
    return _fold_items(items)


def _join(parts: list[tuple[str, float, float]]) -> str:
    """Une átomos (texto, x0, x1). Pone un espacio solo si hay un hueco real entre dos letras o comandos:
    TeX escribe «dx dy» con un espacio fino y «xy» pegado; «27» nunca lleva espacio."""
    out, prev_x1 = "", None
    for text, x0, x1 in parts:
        if out and prev_x1 is not None and out[-1].isalpha() and text and (text[0].isalpha() or text[0] == "\\") \
                and x0 - prev_x1 > 1.0:
            out += " "
        out += text
        prev_x1 = x1
    return out


def _clean_latex(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    # Doble integral con un solo límite (\iint_R): en el PDF son dos ∫ seguidas y el límite va con la primera
    s = re.sub(r"\\int_\{([^{}]*)\}\\int(?![_^])", r"\\iint_{\1}", s)
    s = re.sub(r"\\int\\int(?![_^])", r"\\iint", s)
    # \mathrm{a}\mathrm{b} → \mathrm{ab} (letras romanas consecutivas en distintos trozos)
    prev = None
    while prev != s:
        prev, s = s, re.sub(r"\\mathrm\{([^{}]*)\}\s*\\mathrm\{([^{}]*)\}", r"\\mathrm{\1\2}", s)
    return s


def _relevant_bars(gs: list[Glyph], bars: list[Bar]) -> list[Bar]:
    """Solo cuentan como fracción las reglas dentro de la banda de glifos, con contenido a ambos lados.

    Fuera de la banda: un par de reglas del mismo ancho, una encima y otra debajo, es el marco de una caja (el documento
    las usa para destacar resultados) y se ignora. Una regla suelta pegada a la fórmula podría ser una fracción sin
    denominador o un subrayado: entonces el bloque no se reconstruye."""
    keep, near = [], []
    for b in bars:
        under = [g for g in gs if b.x0 - 1.0 <= g.cx <= b.x1 + 1.0]
        if not under:
            continue
        lo, hi = min(g.y0 for g in under), max(g.y1 for g in under)
        if lo < b.y < hi:
            if not (any(g.cy < b.y for g in under) and any(g.cy > b.y for g in under)):
                raise Unsupported("regla horizontal con contenido solo a un lado")
            keep.append(b)
        elif lo - 6.0 <= b.y <= hi + 6.0:
            near.append((b, b.y < lo, lo - 3.0 <= b.y <= hi + 3.0))   # (regla, arriba, muy pegada)
    for b, above, tight in near:
        frame = any(o is not b and o_above != above and abs(o.x0 - b.x0) < 0.5 and abs(o.x1 - b.x1) < 0.5
                    for o, o_above, _ in near)
        if not frame and tight:
            raise Unsupported("regla junto a la fórmula sin contenido a ambos lados")
    return keep


def reconstruct(gs: list[Glyph], bars: list[Bar]) -> tuple[str, bool, str]:
    """(latex, completa, motivo). Si no es completa, el latex está vacío."""
    try:
        return _clean_latex(_layout(gs, _relevant_bars(gs, bars))), True, ""
    except Unsupported as exc:
        return "", False, str(exc)


# ------------------------------------------------------------- lectura de la página
@dataclass
class GeoEq:
    x0: float
    y0: float
    x1: float
    y1: float
    latex: str
    complete: bool
    why: str
    anchor: str                                   # texto normalizado de la prosa justo encima ('' si no hay)
    frags: list = field(default_factory=list)     # líneas del bloque normalizadas: el texto roto que sustituye


def _norm_text(s: str) -> str:
    """Texto comparable: sin tildes ni signos («Integraci´on» y «Integración» dan lo mismo)."""
    base = "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))
    return re.sub(r"[^0-9a-z]", "", base)


def _lines(page: pymupdf.Page):
    """Líneas del PDF: glifos, texto, si contienen prosa y si parecen matemática."""
    out = []
    for b in page.get_text("rawdict")["blocks"]:
        if b.get("type", 0) != 0:
            continue
        for ln in b["lines"]:
            glyphs, tokens = [], []
            cur: list = []
            for sp in ln["spans"]:
                for c in sp["chars"]:
                    ch = c["c"]
                    if ch.isspace():
                        if cur:
                            tokens.append(cur)
                            cur = []
                        continue
                    o = c["origin"]
                    x0, y0, x1, y1 = c["bbox"]
                    glyphs.append(Glyph(ch, sp["font"], round(sp["size"], 2), x0, y0, x1, y1, o[1]))
                    cur.append(glyphs[-1])
            if cur:
                tokens.append(cur)
            text = "".join(g.c for g in glyphs)
            prose = any(len(re.findall(r"[A-Za-záéíóúñÁÉÍÓÚÑ]", "".join(g.c for g in t))) >= 4
                        and not any(_MATH_FONT.match(g.font) for g in t) for t in tokens)
            mathy = any(_MATH_FONT.match(g.font) for g in glyphs)
            if not glyphs:
                continue
            x0 = min(g.x0 for g in glyphs)
            x1 = max(g.x1 for g in glyphs)
            y0 = min(g.y0 for g in glyphs)
            y1 = max(g.y1 for g in glyphs)
            out.append({"glyphs": glyphs, "text": text, "prose": prose, "mathy": mathy,
                        "rect": pymupdf.Rect(x0, y0, x1, y1)})
    return out


def _table_spans(page: pymupdf.Page) -> list[tuple[float, float]]:
    rows = _rows(page.get_text("words"))
    return [(rows[i]["y0"] - 2, rows[j - 1]["y1"] + 2) for i, j in _find_tables(rows)]


def _is_bar_rect(r: pymupdf.Rect) -> bool:
    """Regla horizontal (fracción, línea de tabla): trazo casi sin altura."""
    return r.height <= 0.8 and r.width >= 1.0


def _group_rect(grp: list[dict]) -> pymupdf.Rect:
    return pymupdf.Rect(min(ln["rect"].x0 for ln in grp), min(ln["rect"].y0 for ln in grp),
                        max(ln["rect"].x1 for ln in grp), max(ln["rect"].y1 for ln in grp))


def _group_lines(body: list[dict], prose: list[dict]) -> list[list[dict]]:
    """Agrupa líneas que se solapan verticalmente (hueco ≤ 3 pt y sin prosa entre medias): un bloque de fórmula."""
    groups: list[list[dict]] = []
    for ln in body:
        if groups:
            grp_rect = _group_rect(groups[-1])
            gap = ln["rect"].y0 - grp_rect.y1
            split = any(grp_rect.y1 - 0.5 <= p["rect"].y0 and p["rect"].y1 <= ln["rect"].y0 + 0.5 for p in prose)
            if gap <= 3.0 and not split:          # 3 pt: los superíndices de una línea distinta quedan dentro
                groups[-1].append(ln)
                continue
        groups.append([ln])
    return groups


def _attach_scripts(body: list[dict], groups: list[list[dict]]) -> None:
    """Límites y subíndices pueden quedar en una línea propia a más distancia: si son pequeños, pegados en horizontal
    y a menos de 12 pt de un bloque, se añaden a ese bloque (si no, la fórmula saldría sin sus límites)."""
    grouped = {id(ln) for grp in groups for ln in grp}
    for ln in body:
        if id(ln) in grouped or not ln["glyphs"] or not ln["mathy"]:
            continue          # solo se acogen líneas con fuente matemática (límites, subíndices), nunca notas o números
        best, best_d = None, 12.0
        for grp in groups:
            gr = _group_rect(grp)
            big = max(g.size for member in grp for g in member["glyphs"])
            if not all(g.size < 0.9 * big for g in ln["glyphs"]):
                continue
            if ln["rect"].x1 < gr.x0 - 12 or ln["rect"].x0 > gr.x1 + 12:
                continue
            d = max(0.0, ln["rect"].y0 - gr.y1, gr.y0 - ln["rect"].y1)
            if d <= best_d:
                best, best_d = grp, d
        if best is not None:
            best.append(ln)
            grouped.add(id(ln))


def _block_to_eq(grp: list[dict], all_bars: list[Bar], prose: list[dict], figs: list[pymupdf.Rect]) -> GeoEq | None:
    """Fórmula (o el motivo de no poder reconstruirla) de un bloque de líneas; None si no es una fórmula."""
    glyphs = [g for ln in grp for g in ln["glyphs"]]
    if len(glyphs) < 3 or not any(ln["mathy"] for ln in grp):
        return None
    rect = pymupdf.Rect(min(ln["rect"].x0 for ln in grp) - 4, min(ln["rect"].y0 for ln in grp) - 3,
                        max(ln["rect"].x1 for ln in grp) + 4, max(ln["rect"].y1 for ln in grp) + 3)
    # Reglas algo fuera del bloque: el marco de una caja tiene su pareja de regla fuera del rectángulo del bloque
    wide = pymupdf.Rect(rect.x0 - 2, rect.y0 - 8, rect.x1 + 2, rect.y1 + 8)
    bars = [b for b in all_bars if wide.contains(pymupdf.Point((b.x0 + b.x1) / 2, b.y))]
    above = [p for p in prose if p["rect"].y1 <= rect.y0 + 1]
    anchor = _norm_text(above[-1]["text"]) if above else ""
    if any(rect.intersects(f) for f in figs):
        # Un gráfico (ejes, curvas, etiquetas de una superficie) no es una fórmula: sus números se confundirían
        latex, ok, why = "", False, "dibujo o gráfico junto a la fórmula"
    else:
        latex, ok, why = reconstruct(glyphs, bars)
    if ok and re.fullmatch(r"[A-Za-z ,.]{1,8}", latex):
        return None           # solo letras («dA», «R»): es prosa con símbolos, no una fórmula en bloque
    frags = [ln["text"] for ln in grp if ln["text"]]
    return GeoEq(rect.x0, rect.y0, rect.x1, rect.y1, latex, ok, why, anchor, frags)


def page_formulas(page: pymupdf.Page) -> list[GeoEq]:
    """Bloques de fórmula de la página con su LaTeX reconstruido (o el motivo de no poder hacerlo).

    Un bloque es un conjunto de líneas que se solapan verticalmente (numerador, barra y denominador se tocan; dos
    ecuaciones distintas no). Las líneas de prosa y las tablas no entran nunca en un bloque."""
    lines = _lines(page)
    tables = _table_spans(page)
    pw, ph = page.rect.width, page.rect.height
    drawings = page.get_drawings()                   # una sola vez por página
    figs = [d["rect"] for d in drawings if not _is_bar_rect(d["rect"]) and d["rect"].width < 0.8 * pw and d["rect"].height < 0.5 * ph]
    all_bars = [Bar(d["rect"].x0, d["rect"].x1, (d["rect"].y0 + d["rect"].y1) / 2) for d in drawings if _is_bar_rect(d["rect"])]
    prose = sorted((ln for ln in lines if ln["prose"]), key=lambda ln: ln["rect"].y0)
    body = [ln for ln in lines if not ln["prose"]
            and not any(a - 1 <= (ln["rect"].y0 + ln["rect"].y1) / 2 <= b + 1 for a, b in tables)]
    body.sort(key=lambda ln: ln["rect"].y0)

    groups = _group_lines(body, prose)
    _attach_scripts(body, groups)
    eqs = (_block_to_eq(grp, all_bars, prose, figs) for grp in groups)
    return sorted((e for e in eqs if e is not None), key=lambda e: e.y0)


# ------------------------------------------------------------- comparación
_DROP = re.compile(r"\\(?:left|right|quad|qquad|displaystyle|textstyle|mathrm|mathit|mathbf|mathsf|text|textbf|"
                   r"operatorname|mathop)(?![A-Za-z])")
_TOKEN = re.compile(r"\\[A-Za-z]+|\\[{}]|\d+(?:\.\d+)?|[A-Za-z]|[^\sA-Za-z\d{}]")
_EQUIV = {"\\times": "*", "\\cdot": "*", "·": "*", "⋅": "*", "×": "*", "°": "\\circ", "◦": "\\circ", "−": "-"}


def canon(latex: str) -> list[str]:
    """Secuencia canónica de una fórmula: sin llaves, espacios ni comandos de estilo. Para comparar dos lecturas."""
    s = re.sub(r"\\[,;!]", " ", latex)
    s = _DROP.sub(" ", s)
    toks = [t for t in _TOKEN.findall(s) if t not in "{}"]
    return [_EQUIV.get(t, t) for t in toks]


def similarity(a: str, b: str) -> float:
    """Jaccard de los símbolos de dos fórmulas (0 a 1); sirve para emparejar lecturas de la misma fórmula."""
    ca, cb = Counter(canon(a)), Counter(canon(b))
    union = sum((ca | cb).values())
    return sum((ca & cb).values()) / union if union else 0.0


def same_formula(a: str, b: str) -> bool:
    return canon(a) == canon(b)


def same_content(a: str, b: str) -> bool:
    """Igual que same_formula pero sin puntuación final (la visión suele perder una coma o un punto; eso no es duda)."""
    punct = {",", ".", ";", ":"}
    return [t for t in canon(a) if t not in punct] == [t for t in canon(b) if t not in punct]


def diff_hint(pdf: str, vis: str) -> str:
    """Qué símbolo difiere entre la lectura del PDF y la de la visión, para que el aviso se pueda comprobar de un vistazo."""
    a = [t for t in canon(pdf) if t not in {",", ".", ";", ":"}]
    b = [t for t in canon(vis) if t not in {",", ".", ";", ":"}]
    parts = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        pdf_part, vis_part = "".join(a[i1:i2]), "".join(b[j1:j2])
        if pdf_part and vis_part:
            parts.append(f"PDF «{pdf_part}» · visión «{vis_part}»")
        elif pdf_part:
            parts.append(f"solo en el PDF: «{pdf_part}»")
        elif vis_part:
            parts.append(f"solo en la visión: «{vis_part}»")
        if len(parts) == 2:
            break
    return "; ".join(parts) or "forma distinta"
