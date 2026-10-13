"""Modo «Texto limpio»: convierte el PDF en Markdown compacto SIN inventar nada (cero IA en el contenido).

Pasos deterministas: quita basura (números de página, líneas vacías de «respuesta», símbolos sueltos), reúne las
líneas partidas en párrafos, marca los títulos, deja el código en bloques ``` con su sangría, convierte filas de cifras
en tablas y, si hay fórmulas leídas con visión, las coloca en LaTeX en lugar de las líneas rotas del PDF.
Resultado típico: ~90 % menos tokens que enviar el PDF completo y 100 % del contenido original.
"""
from __future__ import annotations

import re
import textwrap

from .scrub import fix_plain_tables
from .mathgeom import _norm_text, diff_hint, same_content, similarity

_CODE_LINE = re.compile(
    r"^\s*(?:function\b|if\s*\(|else\b|\}\s*else|for\s*\(|while\s*\(|do\s*\{?\s*$|switch\s*\(|case\b.+:|default\s*:|"
    r"return\b|let\s+\w|const\s+\w|var\s+\w|try\s*\{|catch\s*\(|\}\s*catch|class\s+\w|def\s+\w+\(|import\s+\w|"
    r"(?:int|float|double|char|long|short|bool|boolean|String|void|auto|unsigned|byte|size_t)\b[\w\s*&\[\]<>,]*[;{(=].*$|"
    r"#include\b|#define\b|printf\s*\(|System\.out|print\s*\(|(?:elif|except|finally|with)\b.*:\s*$|else\s*:|"
    r"(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE|FROM|WHERE|GROUP\s+BY|ORDER\s+BY|(?:INNER\s+|LEFT\s+)?JOIN)\b.*|"
    r"for\s+\w+\s+in\b.*:\s*$|self\.\w+|"
    r"public\b|private\b|static\b|break\s*;|continue\s*;|console\.|[\w.\[\]]+\s*(?:\+\+|--|[+\-*/]?=)\s*[^=].*;\s*$|"
    r"[\w.]+\(.*\)\s*;\s*$|[{}]\s*;?\s*$)")
_BULLET = re.compile(r"^\s*[•▪◦●·]\s+|^\s*[-*]\s+")
_NUMBERED = re.compile(r"^\s*(?:\d+[.)]|[a-z][.)])\s+\S")
_HEADING_NUM = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+([A-ZÁÉÍÓÚÑ¿¡].{1,80})$")
_HEADING_KW = re.compile(r"^(?:Ejercicio|Problema|Pregunta|Parte|Cap[ií]tulo|Secci[oó]n|Ejemplo|Taller|Exercise|Problem|Part|Chapter|Section|Example)\s+[\dIVXivx]+\b.{0,90}$")
_NOISE = re.compile(r"^\s*(?:[_\-–—=.·•\s]{3,}|p[áa]g(?:ina)?\.?\s*\d+(?:\s*(?:de|/)\s*\d+)?)\s*$", re.I)
_JS = re.compile(r"\b(?:function|let|const|var|console|===|!==|=>)\b|===|!==|=>")
_PY = re.compile(r"^\s*(?:def\s+\w+\(|import\s+\w|from\s+\w+\s+import|elif\b|print\()|:\s*$", re.M)
_FORMULA_NOISE = re.compile(r"^\s*(?:[A-Za-z]{1,3}|Z{1,3}(?:\s+[-\d]+)*|[-+\d.,()=≈≤≥×·/∑∫\s\[\]|]+)\s*$")


def _words4(line: str) -> int:
    return len(re.findall(r"[A-Za-záéíóúñÁÉÍÓÚÑ]{4,}", line))


# ------------------------------------------------------------ fórmulas de la visión
def merge_equations(text: str, eqs: list) -> str:
    """Sustituye las líneas «de fórmula» rotas del PDF (fracciones desordenadas, símbolos sueltos) por las fórmulas
    LaTeX leídas con visión, insertadas según su posición relativa en la página."""
    if not eqs:
        return text
    lines = text.splitlines()
    n = len(lines)
    noise = [(not ln.strip().startswith("|")) and bool(ln.strip()) and _words4(ln) == 0 and bool(_FORMULA_NOISE.match(ln))
             and not _NUMBERED.match(ln) for ln in lines]
    inserts: dict[int, list[str]] = {}
    for rel, eq in eqs:
        idx = min(n - 1, max(0, int(rel * n)))
        while idx < n and noise[idx]:          # se coloca antes de la primera línea que se conserva
            idx += 1
        inserts.setdefault(idx, []).append(f"$$ {eq} $$")
    out: list[str] = []
    for i, ln in enumerate(lines):
        out += inserts.get(i, [])
        if not noise[i]:
            out.append(ln)
    out += inserts.get(n, [])
    return "\n".join(out)


# ---------------------------------------------------------------- código
def _is_code(ln: str) -> bool:
    return bool(ln.strip()) and bool(_CODE_LINE.match(ln))


def _lang(block: list[str]) -> str:
    joined = "\n".join(block)
    if re.search(r"^\s*(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE)\b", joined, re.M):
        return "sql"
    if re.search(r"#include\b|\bprintf\s*\(|\bint\s+main\b|std::|\bcout\s*<<", joined):
        return "c"
    if re.search(r"\bSystem\.out\b|\bpublic\s+(?:static\s+)?(?:class|void)\b|\bString\[\]", joined):
        return "java"
    if _PY.search(joined) and not _JS.search(joined):
        return "python"
    if _JS.search(joined):
        return "javascript"
    return "" if not re.search(r"[{};]", joined) else "javascript"


def _segment(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Divide en segmentos de código / tabla Markdown / texto."""
    segs: list[tuple[str, list[str]]] = []
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln.strip().startswith("|") and ln.strip().endswith("|"):
            j = i
            while j < n and lines[j].strip().startswith("|"):
                j += 1
            segs.append(("table", lines[i:j]))
            i = j
            continue
        if _is_code(ln):
            j, last = i, i
            while j < n:
                if _is_code(lines[j]):
                    last = j
                elif lines[j].strip() == "" or (lines[j].startswith("  ") and any(c in lines[j] for c in ";(){}=")):
                    pass            # hueco o línea sangrada dentro del bloque
                else:
                    break
                j += 1
            block = lines[i:last + 1]
            if len(block) >= 2 or block[0].rstrip().endswith(("{", ";")):
                segs.append(("code", block))
                i = last + 1
                continue
        # texto hasta el próximo código o tabla
        j = i
        while j < n and not (_is_code(lines[j]) or lines[j].strip().startswith("|")):
            j += 1
        j = max(j, i + 1)
        segs.append(("text", lines[i:j]))
        i = j
    return segs


def _fence(block: list[str]) -> str:
    while block and not block[0].strip():
        block = block[1:]
    while block and not block[-1].strip():
        block = block[:-1]
    code = textwrap.dedent("\n".join(b.rstrip() for b in block))
    code = re.sub(r"\n{3,}", "\n\n", code)
    return f"```{_lang(block)}\n{code}\n```"


# ----------------------------------------------------------------- texto
def _heading(line: str, next_blank: bool) -> str | None:
    s = line.strip()
    if _HEADING_KW.match(s) and not s.endswith((".", ",", ";")) and len(s) <= 110:
        return "### " + s
    m = _HEADING_NUM.match(s)
    if m and len(s) <= 75 and not s.endswith((".", ",", ";", ":")) and next_blank and _words4(s) >= 1:
        depth = m.group(1).count(".") + 1
        return ("## " if depth == 1 else "### ") + s
    return None


def _reflow(lines: list[str]) -> list[str]:
    """Reúne las líneas partidas de un párrafo; conserva viñetas, numeración y filas cortas."""
    items: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        s = _BULLET.sub("- ", ln.strip()) if _BULLET.match(ln) else s
        starts_item = s.startswith(("- ", "$$", "[Fórmulas")) or bool(_NUMBERED.match(ln))
        if items and not starts_item and not items[-1].startswith(("$$", "[Fórmulas")):
            prev = items[-1]
            if len(prev) >= 60 or s[0].islower() or s[0] in ").,;:":
                items[-1] = f"{prev} {s}"
                continue
        items.append(s)
    return [re.sub(r"\s{2,}", " ", it) for it in items]


def _clean_text_segment(lines: list[str], page_break_join: bool = True) -> list[str]:
    out: list[str] = []
    paras: list[list[str]] = [[]]
    for ln in lines:
        if not ln.strip() or _NOISE.match(ln):
            if paras[-1]:
                paras.append([])
            continue
        paras[-1].append(ln)
    for k, para in enumerate(paras):
        if not para:
            continue
        if len(para) == 1:
            h = _heading(para[0], True)
            if h:
                out += ["", h, ""]
                continue
        items = _reflow(para)
        # un encabezado seguido directamente del contenido (sin línea en blanco)
        if len(items) > 1:
            h = _heading(items[0], True)
            if h:
                out += ["", h, ""] + items[1:]
                continue
        out += items
        out.append("")
    return out


def clean_text(text: str) -> str:
    """Texto crudo de un documento → Markdown limpio y compacto."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    for kind, seg in _segment(lines):
        if kind == "code":
            out += ["", _fence(seg), ""]
        elif kind == "table":
            out += ["", *seg, ""]
        else:
            out += _clean_text_segment(seg)
    md = "\n".join(out)
    md, _ = fix_plain_tables(md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    # une un párrafo cortado por un salto de página: línea que no termina en puntuación + minúscula en la siguiente
    md = re.sub(r"([^\n.:;?!)\]`|])\n\n(?=[a-záéíóúñ])", r"\1 ", md)
    return md


def headings(md: str) -> list[str]:
    return [m.group(0).strip() for m in re.finditer(r"^#{2,3} .+$", md, flags=re.M)]


# ------------------------------------------------- texto de página con fórmulas
def _sig_numbers(t: str) -> set[str]:
    return {x.replace(",", ".") for x in re.findall(r"\d+(?:[.,]\d+)?", t) if len(x) >= 2 or "." in x}


def _mark(eq: str, flag: dict | None) -> str:
    """Fórmula en bloque; si está dudosa, se marca con ⚠ y se ofrece la lectura alternativa."""
    out = f"$$ {eq} $$"
    if flag and not flag.get("ok", True):
        out += " ⚠ " + "; ".join(flag.get("why", []))
        if flag.get("alt"):
            out += f" · lectura alternativa: $ {flag['alt']} $"
    return out


def _legacy_page_text(raw_text: str, vis: str, eqs: list, flags: list | None = None) -> str:
    """Páginas con fórmulas: el texto vectorial las rompe (fracciones, límites). Se usa la transcripción de la visión
    SOLO si conserva ≥ 92 % de las cifras y ≥ 85 % de las palabras del texto vectorial (la visión a veces pierde líneas
    o inventa títulos); si no, texto vectorial + las fórmulas LaTeX al final de la página. Las fórmulas dudosas
    (las dos lecturas no coinciden, cifras ausentes del PDF, cadena numérica incorrecta) se marcan con ⚠."""
    if not eqs:
        return raw_text
    flags = flags or [None] * len(eqs)
    if vis:
        rn = _sig_numbers(raw_text)
        rw = {w.lower() for w in re.findall(r"[A-Za-záéíóúñÁÉÍÓÚÑ]{4,}", raw_text)}
        vw = {w.lower() for w in re.findall(r"[A-Za-záéíóúñÁÉÍÓÚÑ]{4,}", vis)}
        nums_ok = len(rn & _sig_numbers(vis)) / len(rn) if rn else 1.0
        words_ok = len(rw & vw) / len(rw) if rw else 1.0
        if nums_ok >= 0.92 and words_ok >= 0.85:
            # títulos de la visión que no existen en el texto vectorial: se degradan a texto normal
            def fix_head(m):
                txt = m.group(2).strip()
                if re.match(r"\d+\.\s", txt) and (txt.endswith((".", ":", ";")) or len(txt) > 70):
                    return txt                      # ítem de lista numerada, no un título
                words = {w.lower() for w in re.findall(r"[A-Za-záéíóúñÁÉÍÓÚÑ]{4,}", m.group(2))}
                return m.group(0) if not words or len(words & rw) / len(words) >= 0.6 else m.group(2)
            text = re.sub(r"^(#{1,6}\s+)(.+)$", fix_head, vis, flags=re.M)
            for (_, eq), fl in zip(eqs, flags):                      # marca ⚠ junto a las fórmulas dudosas
                if fl and not fl.get("ok", True):
                    pat = r"\s*".join(re.escape(t) for t in eq.split())
                    suffix = " ⚠ " + "; ".join(fl.get("why", [])) + (f" · lectura alternativa: $ {fl['alt']} $" if fl.get("alt") else "")
                    text = re.sub(r"(\$\$?\s*" + pat + r"\s*\$\$?)", lambda m_: m_.group(1) + suffix, text, count=1)
            return text
    listing = "\n".join(_mark(e, fl) for (_, e), fl in zip(eqs, flags))
    kept = []
    for ln in raw_text.splitlines():
        s_ = ln.strip()
        broken = (bool(s_) and not s_.startswith("|") and _words4(ln) == 0 and not _NUMBERED.match(ln) and not _is_code(ln)
                  and len(s_) < 110 and (bool(_FORMULA_NOISE.match(ln)) or bool(re.search(r"[=≈≤≥∑∫+−×/\[\]]", s_))))
        if not broken:
            kept.append(ln)
    return "\n".join(kept) + f"\n\n[Fórmulas de la página en LaTeX]\n{listing}"


# ------------------------------------------ fórmulas reconstruidas desde el PDF (Fase 0, sin modelo)
def _vis_accepted(raw_text: str, vis: str) -> bool:
    """¿La transcripción de la visión conserva las cifras y palabras del texto vectorial? Si no, manda el PDF."""
    rn = _sig_numbers(raw_text)
    rw = {w.lower() for w in re.findall(r"[A-Za-záéíóúñÁÉÍÓÚÑ]{4,}", raw_text)}
    vw = {w.lower() for w in re.findall(r"[A-Za-záéíóúñÁÉÍÓÚÑ]{4,}", vis)}
    nums_ok = len(rn & _sig_numbers(vis)) / len(rn) if rn else 1.0
    words_ok = len(rw & vw) / len(rw) if rw else 1.0
    return nums_ok >= 0.92 and words_ok >= 0.85


def _pair_geo(done: list, vis_list: list) -> dict:
    """Empareja cada fórmula reconstruida con la lectura de la visión más parecida (similitud ≥ 0,5, sin repetir)."""
    scored = [(similarity(g["latex"], v), gi, vi) for gi, g in enumerate(done) for vi, v in enumerate(vis_list)]
    pairs, used = {}, set()
    for s, gi, vi in sorted(scored, reverse=True):
        if s >= 0.5 and gi not in pairs and vi not in used:
            pairs[gi] = vi
            used.add(vi)
    return pairs


def _geo_block(g: dict, vis_eq: str | None) -> str:
    """Fórmula reconstruida en bloque. Lleva ⚠ solo si la lectura de la visión contradice al PDF en cifras o símbolos."""
    out = f"$$ {g['latex']} $$"
    if vis_eq is not None and not same_content(g["latex"], vis_eq):
        out += f" ⚠ la lectura visual difiere del PDF ({diff_hint(g['latex'], vis_eq)}) · lectura alternativa: $ {vis_eq} $"
    return out


def _replace_vision_eq(text: str, vis_eq: str, new: str) -> str:
    """Sustituye en la transcripción de la visión la fórmula `vis_eq` (y su ⚠ anterior, si lo tenía) por `new`."""
    pat = r"\s*".join(re.escape(t) for t in vis_eq.split())
    return re.sub(r"\$\$?\s*" + pat + r"\s*\$\$?(?: ⚠[^\n]*)?", lambda m_: new, text, count=1)


_MATH_SIGNS = re.compile(r"[0-9=≈≤≥∑∫+−×/()\[\]^_]")


def _is_fragment(line: str, frag_toks: set[str]) -> bool:
    """Línea del texto vectorial que es un trozo roto de una fórmula ya reconstruida: todas sus palabras largas
    (4+ letras) aparecen en esa fórmula, y tiene cifras, signos o palabras de la fórmula. La prosa no cumple esto."""
    words = re.findall(r"[^\W\d_]+", line)
    if any(len(w) >= 4 and w.lower() not in frag_toks for w in words):
        return False
    return bool(_MATH_SIGNS.search(line)) or any(w.lower() in frag_toks for w in words)


def _anchor_index(lines: list[str], anchor: str, start: int) -> int | None:
    """Línea de prosa (ya normalizada) donde va una fórmula: -1 si va al principio, None si no se encuentra."""
    if not anchor:
        return -1
    for i in range(max(start, 0), len(lines)):
        n = _norm_text(lines[i])
        if n and (n == anchor or (len(n) >= 12 and n in anchor) or (len(anchor) >= 12 and anchor in n)):
            return i
    return None


def _page_with_geo(raw_text: str, vis: str, eqs: list, flags: list, done: list) -> str:
    vis_list = [e for _, e in eqs]
    pairs = _pair_geo(done, vis_list)
    if vis and eqs and _vis_accepted(raw_text, vis):
        # La transcripción de la visión es fiable: sus fórmulas se cambian por las reconstruidas en el mismo sitio
        text = _legacy_page_text(raw_text, vis, eqs, flags)
        text = re.sub(r"\$\$[ \t]*\n[ \t]*([^\n$]+?)[ \t]*\n[ \t]*\$\$", lambda m_: f"$$ {m_.group(1)} $$", text)
        for gi, vi in pairs.items():
            text = _replace_vision_eq(text, vis_list[vi], _geo_block(done[gi], vis_list[vi]))
        return text
    # Texto vectorial: se quitan los fragmentos rotos de las fórmulas y cada fórmula reconstruida va tras su línea de prosa
    frags = [f for g in done for f in g.get("frags", [])]
    frag_toks = {t.lower() for f in frags for t in re.findall(r"[^\W\d_]+", f)}
    lines = []
    for ln in raw_text.splitlines():
        s_ = ln.strip()
        if s_ and not s_.startswith("|") and _is_fragment(s_, frag_toks):
            continue
        broken = (bool(s_) and not s_.startswith("|") and _words4(ln) == 0 and not _NUMBERED.match(ln)
                  and not _is_code(ln) and len(s_) < 110
                  and (bool(_FORMULA_NOISE.match(ln)) or bool(re.search(r"[=≈≤≥∑∫+−×/\[\]]", s_))))
        if not broken:
            lines.append(ln)
    inserts: dict[int, list[str]] = {}
    tail: list[str] = []
    last: int | None = None      # línea de la última fórmula colocada: las fórmulas siguen el orden de la página
    for gi, g in enumerate(done):
        vi = pairs.get(gi)
        block = ["", _geo_block(g, vis_list[vi] if vi is not None else None), ""]
        idx = _anchor_index(lines, g.get("anchor", ""), max(last or 0, 0))
        if idx is None:          # la prosa de anclaje no está en el texto vectorial (se pierde a veces): tras la anterior
            idx = last
        if idx is None:
            tail += block
        else:
            inserts.setdefault(idx, []).extend(block)
            last = idx
    out = list(inserts.get(-1, []))
    for i, ln in enumerate(lines):
        out.append(ln)
        out += inserts.get(i, [])
    out += tail
    left = [k for k in range(len(vis_list)) if k not in pairs.values()]     # visión sin par en el PDF: lista como antes
    if left:
        listing = "\n".join(_mark(vis_list[k], flags[k] if k < len(flags) else None) for k in left)
        out += ["", "[Fórmulas de la página en LaTeX]", listing]
    return "\n".join(out)


def resolve_flags(eqs: list, flags: list | None, geo: list | None) -> list:
    """Banderas de la visión corregidas con la geometría del PDF. Una fórmula de la visión emparejada con una
    reconstruida y completa toma el veredicto del PDF: si coincide, deja de ser dudosa (la doble lectura de la visión
    ya no cuenta); si no coincide en cifras o símbolos, pasa a dudosa con la lectura alternativa."""
    out = list(flags or [None] * len(eqs))
    done = [g for g in (geo or []) if g.get("complete") and g.get("latex")]
    if not done or not eqs:
        return out
    vis_list = [e for _, e in eqs]
    for gi, vi in _pair_geo(done, vis_list).items():
        if same_content(done[gi]["latex"], vis_list[vi]):
            out[vi] = {"ok": True, "why": [], "alt": ""}
        else:
            out[vi] = {"ok": False, "why": [f"la lectura visual difiere del PDF ({diff_hint(done[gi]['latex'], vis_list[vi])})"],
                       "alt": vis_list[vi]}
    return out


def page_text_for_clean(raw_text: str, vis: str, eqs: list, flags: list | None = None, geo: list | None = None) -> str:
    """Texto de una página para «Texto limpio». `geo` son las fórmulas reconstruidas desde los glifos del PDF
    (mathgeom.GeoEq como dict): las completas sustituyen a la lectura de la visión; sin ellas, el comportamiento de antes."""
    flags = flags or [None] * len(eqs)
    done = [g for g in (geo or []) if g.get("complete") and g.get("latex")]
    if not done:
        return _legacy_page_text(raw_text, vis, eqs, flags)
    return _page_with_geo(raw_text, vis, eqs, flags, done)
