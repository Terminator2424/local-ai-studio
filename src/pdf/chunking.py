"""Chunking inteligente: alineado con las secciones del documento y con rango de páginas por fragmento."""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

PAGE_MARK = "⟦p.{n}⟧"
_MARK_RE = re.compile(r"⟦p\.(\d+)⟧")

# Encabezados «fuertes» (siempre abren un fragmento nuevo): «Problema 3.», «Parte I.», «Taller…», «Resumen de resultados»…
_STRONG_RE = re.compile(
    r"^(?:(?:Problema|Parte|Cap[íi]tulo|Secci[oó]n|Ejercicio|Ejemplo|Problem|Part|Chapter|Section|Exercise|Example)\s+[\dIVXivx]+[.:)]?.*"
    r"|(?:Taller de|Respuestas? del|Resumen de|Conclusi[oó]n|Orientaci[oó]n|Preguntas iniciales|Competencia|Resultados de aprendizaje|Regla de)\b[^\n]{0,80})$",
    re.M)
# Encabezados «débiles» (títulos Markdown de la visión): solo parten si el tramo ya es lo bastante largo
_WEAK_RE = re.compile(r"^#{1,4}\s+\S.*$", re.M)


@dataclass
class Chunk:
    index: int
    text: str
    page_start: int
    page_end: int
    title: str = ""
    cont: bool = False       # continuación de la sección del fragmento anterior
    eqs: list = field(default_factory=list)   # fórmulas LaTeX (de la visión) de la zona que cubre el fragmento

    @property
    def pages(self) -> str:
        return str(self.page_start) if self.page_start == self.page_end else f"{self.page_start}-{self.page_end}"


def _clean_title(line: str) -> str:
    return re.sub(r"[*_`#]+", "", line).strip(" .:—-")[:110]


def _section_starts(full: str) -> list[tuple[int, bool]]:
    """(posición, es_fuerte) de las líneas con aspecto de encabezado (excluye las marcas de página)."""
    found: dict[int, bool] = {}
    for rx, strong in ((_WEAK_RE, False), (_STRONG_RE, True)):
        for m in rx.finditer(full):
            if not _MARK_RE.fullmatch(m.group(0).strip()):
                found[m.start()] = strong or found.get(m.start(), False)
    return sorted(found.items())


@dataclass
class _Doc:
    page_content: str
    metadata: dict = field(default_factory=dict)


class RecursiveSplitter:
    """Troceo recursivo por separadores con solape, sin dependencias (sustituye a langchain_text_splitters, cuya
    versión instalada no era compatible con langchain_core y dejaba la app sin poder importar este módulo).
    Mismo contrato que se usa aquí: create_documents([texto]) → documentos con metadata['start_index']."""

    def __init__(self, chunk_size: int, chunk_overlap: int, separators: list[str]):
        self.size = chunk_size
        self.overlap = chunk_overlap
        self.seps = list(separators)

    def _pieces(self, text: str, base: int, seps: list[str]) -> list[tuple[str, int]]:
        """Trozos de como mucho `size` caracteres; el separador queda al final de su trozo (así no se pierde nada)."""
        if len(text) <= self.size:
            return [(text, base)]
        sep = next((s for s in seps if s == "" or s in text), "")
        if sep == "":
            return [(text[i:i + self.size], base + i) for i in range(0, len(text), self.size)]
        rest = seps[seps.index(sep) + 1:]
        parts, pos = [], 0
        for m in re.finditer(re.escape(sep), text):
            parts.append((text[pos:m.end()], base + pos))
            pos = m.end()
        if pos < len(text):
            parts.append((text[pos:], base + pos))
        out = []
        for p, st in parts:
            out += [(p, st)] if len(p) <= self.size else self._pieces(p, st, rest)
        return out

    def create_documents(self, texts: list[str]) -> list[_Doc]:
        docs: list[_Doc] = []
        for text in texts:
            cur: list[tuple[str, int]] = []
            cur_len = 0
            for p, st in self._pieces(text, 0, self.seps):
                if cur and cur_len + len(p) > self.size:
                    docs.append(self._doc(cur))
                    # El solape: se descartan trozos por delante mientras no quepa el nuevo o pase del solape
                    while cur and (cur_len > self.overlap or cur_len + len(p) > self.size):
                        cur_len -= len(cur.pop(0)[0])
                cur.append((p, st))
                cur_len += len(p)
            if cur:
                docs.append(self._doc(cur))
        return [d for d in docs if d.page_content.strip()]

    @staticmethod
    def _doc(pieces: list[tuple[str, int]]) -> _Doc:
        text = "".join(p for p, _ in pieces)
        lead = len(text) - len(text.lstrip())
        return _Doc(text.strip(), {"start_index": pieces[0][1] + lead})


def build_chunks(pages: list, chunk_size: int, chunk_overlap: int, separators: list[str],
                 section_aware: bool = True) -> list[Chunk]:
    """pages: lista de objetos con .number y .text. Inserta marcas de página y divide por secciones."""
    pieces, marks, blocks = [], [], []
    pos = 0
    for p in pages:
        if not p.text.strip():
            continue
        block = f"{PAGE_MARK.format(n=p.number)}\n{p.text.strip()}\n\n"
        marks.append((pos, p.number))
        blocks.append((pos, pos + len(block), len(PAGE_MARK.format(n=p.number)) + 1, len(p.text.strip()),
                       getattr(p, "eqs", None) or []))
        pieces.append(block)
        pos += len(block)
    full = "".join(pieces)
    if not full.strip():
        return []

    offsets = [s for s, _ in marks]
    numbers = [n for _, n in marks]

    def page_at(position: int) -> int:
        return numbers[max(0, bisect.bisect_right(offsets, position) - 1)]

    def equations_for(a: int, b: int) -> list[str]:
        """Fórmulas de la imagen cuya posición relativa en la página cae dentro del tramo [a, b] del texto."""
        out: list[str] = []
        for bs, be, mlen, plen, eqs in blocks:
            if not eqs or be <= a or bs >= b:
                continue
            lo = max(a, bs + mlen) - bs - mlen
            hi = min(b, be) - bs - mlen
            lo_r, hi_r = lo / max(1, plen), hi / max(1, plen)
            out += [e for rel, e in eqs if lo_r - 0.06 <= rel <= hi_r + 0.06 and e not in out]
        return out

    # --- 1. Secciones -------------------------------------------------------
    min_len = max(300, int(chunk_size * 0.3))
    bounds = [0]
    for start, strong in (_section_starts(full) if section_aware else []):
        span = start - bounds[-1]
        if start > bounds[-1] and (span >= min_len or (strong and span >= 120)):
            bounds.append(start)
    if len(full) - bounds[-1] < 120 and len(bounds) > 1:   # último tramo diminuto: se une al anterior
        bounds.pop()
    bounds.append(len(full))
    sections = list(zip(bounds[:-1], bounds[1:]))

    splitter = RecursiveSplitter(chunk_size=chunk_size, chunk_overlap=min(chunk_overlap, chunk_size // 2),
                                 separators=separators)

    # --- 2. Fragmentos por sección ---------------------------------------------
    chunks: list[Chunk] = []
    for s_start, s_end in sections:
        sec_text = full[s_start:s_end]
        first_line = next((ln for ln in sec_text.splitlines() if ln.strip() and not _MARK_RE.fullmatch(ln.strip())), "")
        fl = first_line.strip()
        title = _clean_title(fl) if (_STRONG_RE.fullmatch(fl) or _WEAK_RE.fullmatch(fl)) else ""
        if len(sec_text) <= chunk_size * 1.3:
            parts = [(sec_text, s_start)]
        else:
            docs = splitter.create_documents([sec_text])
            parts = [(d.page_content, s_start + d.metadata.get("start_index", 0)) for d in docs]
        for j, (text, abs_start) in enumerate(parts):
            if not text.strip():
                continue
            p_start = page_at(abs_start)
            p_end = page_at(abs_start + len(text) - 1)
            if not _MARK_RE.match(text.lstrip()):
                text = f"{PAGE_MARK.format(n=p_start)}\n{text}"
            chunks.append(Chunk(len(chunks) + 1, text.strip(), p_start, p_end, title, cont=j > 0,
                                eqs=equations_for(abs_start, abs_start + len(text))))
    return chunks
