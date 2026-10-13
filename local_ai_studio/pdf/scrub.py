"""Saneado DETERMINISTA de las notas: lo que el modelo no puede garantizar, el código lo comprueba contra la fuente.

Reglas (todas verificables sin IA):
  1. Fórmulas: si una fórmula de la nota no existe en las leídas de la imagen pero se parece mucho a una de ellas
     (límites permutados, factor 1/3 o 1/9 perdido), se sustituye por la de la fuente.
  2. Cifras: se elimina cualquier línea que contenga cifras que NO aparecen en el texto fuente (cálculos o tablas
     inventados por el modelo). Más vale una línea de menos que un dato falso.
  3. Numeración inventada («Tabla 1», «Figura 2») que la fuente no contiene.
  4. Frases de relleno («No se proporciona…») que no están en la fuente.
  5. Tablas numéricas de la fuente que la nota omitió: se añaden tal cual.
"""
from __future__ import annotations

import difflib
import re

from .textproc import _CITE_RE, _significant, number_tokens

_DISPLAY_RE = re.compile(r"\$\$\s*(.*?)\s*\$\$|\\\[\s*(.*?)\s*\\\]", re.S)
_SPAN_RE = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)|(?<![\\$])\$([^$\n]+?)\$", re.S)
_FILLER_RE = re.compile(
    r"\bno (?:se (?:proporciona|proporcionan|muestra|muestran|menciona|mencionan|presenta|presentan|incluye|incluyen|"
    r"describe|describen|indica|indican|observan?|encontr\w+)|hay|existen?|contiene)\b"
    r"|\bninguna? (?:figura|tabla|conclusi\w+)\b|\bthere (?:is|are) no\b|\bnot (?:provided|mentioned|shown)\b", re.I)
# Eco de las instrucciones del prompt: «(nombres, cifras, fechas…)», «(ningún dato numérico…)», «(ninguna fórmula…)»
_META_PAREN_RE = re.compile(
    r"^\s*(?:[-*]\s*)?\((?:ning[úu]n|ninguna|ningunos|sin\s|no\s|—|nombres,|problemas,|qu[eé]\s+muestran|en\s+LaTeX|"
    r"names,|problems,|what\s+they\s+show|in\s+LaTeX|none|no\s)[^\n]{0,240}\)\s*\.?\s*$", re.I)
_NUMBERED_RE = re.compile(r"\b(Tabla|Figura|Gráfico|Grafico|Table|Figure|Chart)\s+(\d+)\b")
_PAGE_MARK_RE = re.compile(r"⟦p\.\d+⟧")


def _norm_formula(f: str) -> str:
    f = re.sub(r"\\(?:left|right|displaystyle|,|;|!|:|quad|qquad)", "", f)
    f = f.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    return re.sub(r"[\s{}]+", "", f)


def _fold(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


# ----------------------------------------------------------------- 1. fórmulas
def repair_formulas(note: str, eqs: list[str], threshold: float = 0.86) -> tuple[str, int]:
    """Sustituye fórmulas casi idénticas a una leída de la imagen (pero no iguales) por la de la imagen."""
    if not eqs:
        return note, 0
    ref = [(_norm_formula(e), e) for e in eqs]
    ref_set = {r for r, _ in ref}
    count = 0

    def rep(m):
        nonlocal count
        idx = next(i for i in range(1, 5) if m.group(i) is not None)
        body = m.group(idx)
        nb = _norm_formula(body)
        near = any(nb in r or r in nb for r, _ in ref if abs(len(r) - len(nb)) < 4 and min(len(r), len(nb)) > 8)
        if len(nb) < 8 or nb in ref_set or near:
            return m.group(0)
        best, best_ratio = None, 0.0
        for r, orig in ref:
            if len(r) < 8 or min(len(r), len(nb)) / max(len(r), len(nb)) < 0.6:
                continue
            ratio = difflib.SequenceMatcher(None, nb, r).ratio()
            same_chars = sorted(nb) == sorted(r)               # mismos símbolos en otro orden (límites permutados)
            if same_chars:
                ratio = max(ratio, 0.99)
            elif len(nb) > 1.1 * len(r):
                continue            # la nota amplía la fórmula de la fuente (p. ej. añade «= 171»): no se toca
            if ratio > best_ratio:
                best, best_ratio = orig, ratio
        if best is not None and best_ratio >= threshold:
            count += 1
            whole = m.group(0)
            return whole.replace(body, f" {best} " if whole.startswith(("$$", "\\[")) else best, 1)
        return m.group(0)

    return _SPAN_RE.sub(rep, note), count


# ------------------------------------------- 1b. fórmulas ajenas a la fuente
def drop_foreign_formulas(note: str, eqs: list[str], source: str = "") -> tuple[str, int]:
    """Elimina fórmulas largas de la nota que no se parecen a NINGUNA fórmula de la fuente (el modelo las inventó).
    Sin fórmulas de la imagen, aún se descartan sumatorios (\\sum) si la fuente no contiene ninguno."""
    ref = [_norm_formula(e) for e in eqs]
    source_has_sum = "∑" in source or any("\\sum" in e for e in eqs)
    source_has_math = bool(eqs) or bool(re.search(r"[=≈∫∑]|\bZ{1,3}\b", source))
    dropped = 0
    out = []
    for line in _collapse_display(note).splitlines():
        m = _SPAN_RE.search(line)
        if m:
            body = next(g for g in m.groups() if g is not None)
            nb = _norm_formula(body)
            covers = len(m.group(0)) >= 0.6 * len(line.strip())
            if covers and "\\sum" in body and not source_has_sum:
                dropped += 1
                continue
            if covers and not source_has_math and re.search(r"\\(?:int|iint|sum|frac)", body) and len(nb) >= 30:
                dropped += 1          # el fragmento no contiene matemática: la fórmula es invención del modelo
                continue
            if covers and ref and len(nb) >= 45:
                best = max((difflib.SequenceMatcher(None, nb, r).ratio() for r in ref), default=1.0)
                if best < 0.55:
                    dropped += 1
                    continue
        out.append(line)
    return "\n".join(out), dropped


# ------------------------------------------------------- 2-4. líneas y numeración
def _collapse_display(note: str) -> str:
    """Pasa cada fórmula en bloque a una sola línea, para poder tratarla como unidad."""
    def one(m):
        body = next(g for g in m.groups() if g is not None)
        body = re.sub(r"\s+", " ", body)
        return f"$$ {body} $$" if m.group(0).startswith("$$") else f"\\[ {body} \\]"
    return _DISPLAY_RE.sub(one, note)


def scrub_lines(note: str, source: str) -> tuple[str, dict]:
    src_nums = number_tokens(source)
    src_fold = _fold(source)
    stats = {"numbers": 0, "filler": 0, "numbering": 0}
    out = []
    for line in _collapse_display(note).splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            out.append(line)
            continue
        if _META_PAREN_RE.match(s):
            stats["filler"] += 1
            continue
        # 4. relleno que la fuente no dice
        fm = _FILLER_RE.search(s)
        if fm and len(s) < 300:
            phrase = " ".join(re.sub(r"[*_`]", "", s[fm.start():]).split()[:4]).lower()
            if phrase not in src_fold:
                stats["filler"] += 1
                continue
        # 2. cifras sin respaldo
        bad = {t for t in number_tokens(_CITE_RE.sub("", s)) if _significant(t)} - src_nums
        if bad:
            stats["numbers"] += 1
            continue
        # 3. numeración inventada
        def unnumber(m):
            if _fold(m.group(0)) in src_fold:
                return m.group(0)
            stats["numbering"] += 1
            return m.group(1)
        out.append(_NUMBERED_RE.sub(unnumber, line))
    text = "\n".join(out)
    # tablas que se quedaron sin filas de datos
    text = re.sub(r"^[ \t]*[-*]?[ \t]*\|[^\n]*\|?[ \t]*\n[ \t]*\|[ \t:|-]+\|?[ \t]*(?:\n(?![ \t]*\|)|\Z)", "", text, flags=re.M)
    text = _drop_orphan_labels(text)
    text = re.sub(r"^[ \t]*(?:—|-{3,})[ \t]*$", "", text, flags=re.M)       # «—» y «---» sueltos
    return re.sub(r"\n{3,}", "\n\n", text).strip(), stats


_LABEL_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:#{3,4}\s+.+|\*\*[^*\n]+\*\*:?)\s*$")


def _drop_orphan_labels(text: str) -> str:
    """Quita títulos/etiquetas en negrita que se quedaron sin contenido tras el saneado."""
    lines = text.splitlines()
    keep = []
    for i, ln in enumerate(lines):
        if _LABEL_RE.match(ln):
            nxt = next((nl for nl in lines[i + 1:] if nl.strip()), None)
            if nxt is None or _LABEL_RE.match(nxt):
                continue
        keep.append(ln)
    return "\n".join(keep)


# ----------------------------------------------------------- 5. tablas omitidas
def _numeric_rows(source: str) -> list[list[list[str]]]:
    """Bloques de ≥3 líneas consecutivas con ≥4 números (cada bloque = lista de filas de tokens)."""
    blocks, cur = [], []
    for line in source.splitlines():
        line = _PAGE_MARK_RE.sub("", line).strip()
        toks = line.split()
        nums = [t for t in toks if re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", t)]
        if len(nums) >= 4 and len(nums) >= 0.8 * len(toks) and len(toks) <= 14:
            cur.append(toks)
        else:
            if len(cur) >= 3:
                blocks.append(cur)
            cur = []
    if len(cur) >= 3:
        blocks.append(cur)
    return blocks


def append_missing_tables(note: str, source: str, label: str = "Tabla de la fuente") -> tuple[str, int]:
    """Añade al final las tablas numéricas de la fuente de las que la nota no conserva al menos la mitad de las filas."""
    flat = " " + re.sub(r"[|&\\\s]+", " ", re.sub(r"\$[^$]*\$", lambda m: m.group(0).replace(",", " "), note)) + " "
    added = 0
    extra = []
    for rows in _numeric_rows(source):
        present = sum(1 for r in rows if " " + " ".join(r) + " " in flat)
        if present >= 0.5 * len(rows):
            continue
        width = max(len(r) for r in rows)
        md = ["| " + " | ".join([""] * (width - len(rows[0])) + rows[0]) + " |", "|" + "---|" * width]
        md += ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows[1:]]
        extra.append("\n".join(md))
        added += 1
    if extra:
        note = note.rstrip() + f"\n\n**{label}**\n\n" + "\n\n".join(extra)
    return note, added


# ------------------------------------------- tablas en texto plano → Markdown
_CELL_RE = re.compile(r"^(?:[-+]?\d+(?:[.,]\d+)?|[A-Za-z\\]{1,4}(?:\\[A-Za-z])?)$")


def fix_plain_tables(note: str) -> tuple[str, int]:
    """Bloques de ≥3 líneas de cifras/etiquetas cortas sin barras (tablas pegadas como texto) → tabla Markdown."""
    lines = note.splitlines()
    out: list[str] = []
    i, fixed = 0, 0
    while i < len(lines):
        j = i
        rows: list[list[str]] = []
        while j < len(lines):
            s = re.sub(r"^[-*]\s+", "", lines[j].strip())      # filas pegadas como viñetas: «- 8 5.4 5.7 ...»
            toks = s.split()
            if (s and not s.startswith(("|", "#", ">", "$", "\\")) and len(toks) >= 4
                    and all(_CELL_RE.match(t) for t in toks) and any(re.match(r"[-+]?\d", t) for t in toks)):
                rows.append(toks)
                j += 1
            else:
                break
        if len(rows) >= 3:
            width = max(len(r) for r in rows)
            if len(rows[0]) == width - 1 and out:            # esquina «y\x» escrita en la línea anterior
                m_c = re.match(r"^[-*\s$]*([A-Za-z])\s*(?:\\backslash|\\|/)\s*([A-Za-z])[$\s]*$", out[-1])
                if m_c:
                    rows[0] = [f"{m_c.group(1)}\\{m_c.group(2)}"] + rows[0]
                    out.pop()
            out.append("| " + " | ".join([""] * (width - len(rows[0])) + rows[0]) + " |")
            out.append("|" + "---|" * width)
            out += ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows[1:]]
            fixed += 1
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out), fixed


# ------------------------------------------------------------------ conjunto
def scrub_note(note: str, source: str, eqs: list[str]) -> tuple[str, dict]:
    note, fixed = repair_formulas(note, eqs)
    note, tables_fixed = fix_plain_tables(note)
    note, foreign = drop_foreign_formulas(note, eqs, source)
    note, stats = scrub_lines(note, source)
    note, added = append_missing_tables(note, source)
    stats.update(formulas=fixed, foreign=foreign, tables=added, plain_tables=tables_fixed)
    return note, stats


def scrub_text_numbers(text: str, source: str) -> tuple[str, int]:
    """Para el resumen ejecutivo: elimina líneas con cifras que la fuente no contiene."""
    cleaned, stats = scrub_lines(text, source)
    return cleaned, stats["numbers"]
