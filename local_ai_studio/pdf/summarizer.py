"""Pipeline Map + Verificación contra la fuente + Síntesis con Self-Refine (100 % local con Ollama).

Idea clave: un modelo de 7B alucina cuando REESCRIBE mucho texto técnico de golpe (mezcla problemas, inventa
numeraciones, pierde factores en las fórmulas). Por eso:

  1. Extracción: texto vectorial (palabras y cifras exactas) + fórmulas LaTeX leídas con visión (fracciones y
     límites que PyMuPDF desordena) + OCR con visión en páginas escaneadas.
  2. Chunking por SECCIONES del documento (Problema 1, Problema 2...), con rango de páginas.
  3. Gráficos descritos con el modelo de visión.
  4. MAP: nota exhaustiva por fragmento.
  5. VERIFICACIÓN: cada nota se audita contra el texto de SU fragmento, con avisos automáticos de cifras y términos
     que no existen en la fuente. Solo se acepta la versión corregida si no empeora.
  6. SÍNTESIS: el cuerpo del resumen son las notas verificadas (nada se reescribe ni se pierde); el modelo solo
     redacta el resumen ejecutivo y las conclusiones generales, con Self-Refine (crítica + mejora).
  7. Auditoría numérica determinista (cobertura de cifras y cifras sin respaldo).

El nivel «Ultra-corto» usa la ruta clásica Map-Reduce (fusión jerárquica + Self-Refine).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from ..core import cache
from .chunking import Chunk, build_chunks
from .extractor import PageData, extract_pages, open_pdf, render_page
from .notes import est_tokens, nfc, parse_note, unique_lines
from .scrub import scrub_note, scrub_text_numbers
from .cleaner import clean_text, headings, page_text_for_clean, resolve_flags
from ..core.mdview import convert_arrays
from .textproc import (audit_numbers, clean_output, compact_latex, section_text, unknown_terms,
                       unsupported_numbers)
from ..core.errors import Cancelled, PDFError
from ..core.ollama_client import chat_stream, check_ollama
from ..core.paths import SUMMARIES_DIR
from ..core.textutil import LANG_NAMES_EN, LANG_NAMES_ES, detect_language, fill, fmt_time, safe_name, sha256_file
from .verify import Fidelity, check_code_block, fenced_blocks, mark_numeric_errors
from .vision import Vision

log = logging.getLogger(__name__)

PIPELINE_VERSION = "17"   # súbelo si cambias prompts/lógica del resumen para invalidar cachés viejas
EXTRACT_VERSION = "11"   # súbelo si cambias la extracción de páginas

_FIGURE_RE = re.compile(
    r"\b(fig(?:ura|ure|\.)?s?|gr[áa]fic[oa]|chart|diagram[a]?|esquema|ilustraci[óo]n|plot|"
    r"representaci[óo]n\s+3D|3D\s+(?:plot|representation)|superficie|surface)\b", re.I)


_STAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")        # la fecha de generación cambia siempre; las cifras del contenido NO se ignoran
KEEP_VERSIONS = 10


def archive_previous(out: Path, new_md: str) -> Path | None:
    """Antes de sobrescribir un resumen, guarda el anterior en resumenes/historial/ (con la fecha en el nombre) si su contenido
    cambió de verdad: así volver a resumir un PDF nunca destruye la versión previa. Conserva las últimas `KEEP_VERSIONS`."""
    if not out.is_file():
        return None
    old = out.read_text(encoding="utf-8")
    if _STAMP.sub("", old) == _STAMP.sub("", new_md):
        return None
    folder = out.parent / "historial"
    folder.mkdir(exist_ok=True)
    stamp = datetime.fromtimestamp(out.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
    dest = folder / f"{out.stem}_{stamp}.md"
    n = 1
    while dest.exists():
        dest = folder / f"{out.stem}_{stamp}_{n}.md"
        n += 1
    dest.write_text(old, encoding="utf-8")
    for extra in sorted(folder.glob(f"{out.stem}_*.md"))[:-KEEP_VERSIONS]:
        extra.unlink(missing_ok=True)
    return dest


class UI:
    """Interfaz de callbacks; la GUI la sustituye por una versión que usa colas."""
    def progress(self, frac: float, msg: str): ...
    def log(self, msg: str): ...
    def live(self, text: str): ...
    def phase(self, key: str): ...
    def stats(self, **kw): ...


def extraction_key(cfg: dict, math_vision: bool, double_vision: bool = True) -> str:
    return cache.params_hash({"v": EXTRACT_VERSION, "mv": math_vision, "dv": double_vision,
                              "vm": cfg["ollama"]["vision_model"], "e": cfg["extraction"]})


class Summarizer:
    def __init__(self, cfg: dict, settings: dict, ui: UI, cancel: threading.Event):
        """
        settings: detail ('Ultra-corto'|'Normal'|'Detallado'), language ('auto'|'es'|'en'),
                  chunk_size, chunk_overlap, refine_iterations, use_cache, describe_figures,
                  math_vision, verify
        """
        self.cfg, self.s, self.ui, self.cancel = cfg, settings, ui, cancel
        self.level = cfg["detail_levels"][settings["detail"]]
        self.level_key = self.level["key"]
        self.text_model = cfg["ollama"]["text_model"]
        self.text_options = dict(cfg["ollama"]["text_options"])
        self.prompts: dict = {}
        self.lang_name = ""
        self.doc_title = ""
        self.out_lang = "es"

    # ------------------------------------------------------------ utilidades
    def _check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def _log(self, msg: str):
        log.info(msg)
        self.ui.log(msg)

    def _ask(self, user_prompt: str, num_predict: int, label: str, clean: bool = True) -> str:
        """Llamada al modelo de texto con el prompt de sistema del idioma activo."""
        self._check()
        messages = [
            {"role": "system", "content": fill(self.prompts["system"], lang_name=self.lang_name)},
            {"role": "user", "content": user_prompt},
        ]
        options = dict(self.text_options, num_predict=num_predict)
        t0 = time.time()
        self._log(f"▶ {label} ...")
        out = chat_stream(self.cfg, self.text_model, messages, options, self.cancel, self.ui.live)
        if clean:
            out = clean_output(out)
        self._log(f"✔ {label}: {len(out)} caracteres en {fmt_time(time.time() - t0)}")
        return out

    def _select_prompts(self, doc_lang: str):
        """Elige idioma de salida y juego de prompts (es / en)."""
        want = self.s["language"]
        out_lang = doc_lang if want == "auto" else want
        ui_lang = "es" if out_lang == "es" else "en"
        self.prompts = self.cfg["prompts"][ui_lang]
        names = LANG_NAMES_ES if ui_lang == "es" else LANG_NAMES_EN
        self.lang_name = names.get(out_lang, out_lang)
        self.out_lang = out_lang

    # ------------------------------------------------------------- principal
    def _validate_pdf(self, pdf_path: str) -> None:
        """Rechaza lo que no se puede o no se debe procesar ANTES de gastar tiempo de IA."""
        if not Path(pdf_path).is_file():
            raise PDFError("El archivo no existe.")
        lim = self.cfg.get("limits", {})
        size_mb = Path(pdf_path).stat().st_size / 1024 ** 2
        if size_mb > float(lim.get("max_pdf_mb", 200)):
            raise PDFError(f"El PDF pesa {size_mb:.0f} MB (límite {lim.get('max_pdf_mb', 200)} MB, ver config.yaml → limits).")
        doc = open_pdf(pdf_path)       # corrupto o cifrado: falla aquí con un mensaje claro
        try:
            n_pages = doc.page_count
        finally:
            doc.close()
        self._check_page_limit(n_pages)

    def _check_page_limit(self, n_pages: int) -> None:
        limit = int(self.cfg.get("limits", {}).get("max_pages", 800))
        if n_pages > limit:
            raise PDFError(f"El PDF tiene {n_pages} páginas (límite {limit}, ver config.yaml → limits).")

    def _token_stats(self, pages, md: str, n_pages: int) -> dict:
        """Ahorro de tokens: el objetivo de la app es que un LLM lea el resumen en vez del PDF completo."""
        tok_txt = est_tokens("\n".join(p.raw or p.text for p in pages))
        per_page = int(self.cfg.get("tokens", {}).get("pdf_page_overhead", 1500))
        tok_pdf = tok_txt + n_pages * per_page          # un LLM que recibe el PDF lee texto + imagen de cada página
        tok_md = est_tokens(md)
        return {"summary": tok_md, "pdf_text": tok_txt, "pdf": tok_pdf,
                "saving": round(100 * (1 - tok_md / tok_pdf), 1) if tok_pdf else 0.0,
                "saving_text": round(100 * (1 - tok_md / tok_txt), 1) if tok_txt else 0.0}

    def _publish(self, result: dict, md: str, name: str, pdf_hash: str, phash: str) -> dict:
        """Guarda el resultado en la caché y el Markdown en resumenes/; avisa a la interfaz de que terminó."""
        cache.save(pdf_hash, "final", phash, result)
        out = SUMMARIES_DIR / f"{safe_name(Path(name).stem)}_resumen.md"
        archive_previous(out, md)
        out.write_text(md, encoding="utf-8")
        result["saved_md"] = str(out)
        self._log(f"✔ Guardado en {out}  (total {fmt_time(result['elapsed'])})")
        self.ui.phase("done")
        self.ui.progress(1.0, "Listo")
        return result

    def _extract(self, pdf_path: str, pdf_hash: str) -> tuple[list[PageData], str, int]:
        """Páginas, título y nº de páginas: de la caché si se puede; si no, extrayendo (con visión donde haga falta)."""
        s = self.s
        math_vision = bool(s.get("math_vision", True))
        double_vision = bool(s.get("double_vision", self.cfg["extraction"].get("double_vision", True)))
        ekey = extraction_key(self.cfg, math_vision, double_vision)
        cached_pages = cache.load(pdf_hash, "pages", ekey) if s["use_cache"] else None
        if cached_pages:
            self._log("↺ Extracción recuperada de caché")
            return [PageData(**p) for p in cached_pages["pages"]], cached_pages["title"], cached_pages["n_pages"]
        vision0 = Vision(self.cfg, self.cfg["prompts"]["es"], "el idioma original", self.cancel, self.ui.live)

        def prog_extract(i, total, msg):
            self.ui.progress(0.15 * i / max(1, total), msg)

        pages, title, n_pages = extract_pages(pdf_path, pdf_hash, self.cfg, vision0, prog_extract, self.cancel,
                                              math_vision=math_vision, phase=self.ui.phase, double_vision=double_vision)
        cache.save(pdf_hash, "pages", ekey, {"pages": [p.__dict__ for p in pages], "title": title, "n_pages": n_pages})
        return pages, title, n_pages

    def process(self, pdf_path: str) -> dict:
        t_start = time.time()
        pdf_path = str(pdf_path)
        name = Path(pdf_path).name
        if not Path(pdf_path).is_file():
            raise PDFError("El archivo no existe.")
        self._log(f"═══ {name} ═══")
        self._validate_pdf(pdf_path)
        check_ollama(self.cfg, need_vision=True)

        pdf_hash = sha256_file(pdf_path)
        s = self.s
        verify = bool(s.get("verify", True)) and bool(self.level.get("verify", True))
        pparams = {
            "v": PIPELINE_VERSION, "detail": s["detail"], "lang": s["language"], "cs": s["chunk_size"],
            "co": s["chunk_overlap"], "it": s["refine_iterations"], "fig": s["describe_figures"],
            "mv": s.get("math_vision", True), "ver": verify, "dv": s.get("double_vision", True),
            "tm": self.text_model, "vm": self.cfg["ollama"]["vision_model"],
        }
        phash = cache.params_hash(pparams)
        if s["use_cache"]:
            hit = cache.load(pdf_hash, "final", phash)
            if hit:
                self._log("✔ Resumen encontrado en caché (mismo PDF y mismos ajustes). Desmarca «Usar caché» para regenerarlo.")
                self.ui.phase("done")
                self.ui.progress(1.0, "Listo (desde caché)")
                self.ui.stats(pages=hit.get("pages"), chunks=hit.get("chunks"), figures=hit.get("figures"),
                              quality=hit.get("quality"), tokens=hit.get("tokens"))
                hit["from_cache"] = True
                return hit

        # --- 1. Extracción -------------------------------------------------
        pages, title, n_pages = self._extract(pdf_path, pdf_hash)
        self._check_page_limit(n_pages)
        total_chars = sum(len(p.text) for p in pages)
        vis_pages = [p.number for p in pages if p.source in ("visión", "texto+fórmulas")]
        if total_chars < 50:
            raise PDFError("No se pudo extraer texto del PDF (ni con OCR). ¿Está vacío o dañado?")
        self.doc_title = title
        self.src_tokens = est_tokens("\n".join(p.raw or p.text for p in pages))
        doc_lang = detect_language("\n".join(p.raw or p.text for p in pages))
        self._select_prompts(doc_lang)
        self._log(f"Páginas: {n_pages} · caracteres: {total_chars:,} · leídas con visión: {len(vis_pages)} · "
                  f"idioma detectado: {doc_lang} · salida: {self.lang_name}")

        if self.level_key == "clean":
            return self._process_clean(name, pages, n_pages, vis_pages, title, t_start, pdf_hash, phash)

        # --- 2. Chunking por secciones ------------------------------------
        self.ui.progress(0.15, "Dividiendo en fragmentos...")
        chunks = build_chunks(pages, int(s["chunk_size"]), int(s["chunk_overlap"]), self.cfg["chunking"]["separators"])
        n = len(chunks)
        self._log(f"Fragmentos: {n} (tamaño {s['chunk_size']}, solape {s['chunk_overlap']}, alineados con las secciones)")
        if n == 0:
            raise PDFError("No hay texto que resumir.")
        self.ui.stats(pages=n_pages, chunks=n)

        # --- 3. Gráficos con visión ---------------------------------------
        vision = Vision(self.cfg, self.prompts, self.lang_name, self.cancel, self.ui.live)
        figures = self._describe_figures(pdf_path, pdf_hash, pages, chunks, vision) if s["describe_figures"] else {}
        self.ui.stats(figures=len(figures))

        # --- 4. MAP ---------------------------------------------------------
        self.ui.phase("map")
        notes = self._map_phase(pdf_hash, phash, chunks, figures)

        # --- 5-7. Verificación + síntesis ------------------------------------
        source_text = "\n".join(p.text + "\n" + p.raw + "\n" + "\n".join(e for _, e in p.eqs) for p in pages)
        if self.level_key == "ultra":
            summary = self._ultra(chunks, notes, n_pages)
        else:
            if verify:
                notes = self._verify_phase(pdf_hash, phash, chunks, notes)
            notes = self._scrub_phase(chunks, notes)
            summary = self._synthesize(chunks, notes, n_pages, source_text, figures)

        # --- Ensamblado y auditoría numérica --------------------------------
        md, quality = self._assemble(name, n_pages, vis_pages, summary, figures, source_text)
        self._log(f"Auditoría: cobertura de cifras {quality['coverage']} % · cifras del resumen sin respaldo: "
                  f"{len(quality['unsupported'])}" + (f" ({', '.join(quality['unsupported'][:12])})" if quality["unsupported"] else ""))
        tokens = self._token_stats(pages, md, n_pages)
        tok_md, tok_pdf, tok_txt = tokens["summary"], tokens["pdf"], tokens["pdf_text"]
        lb = self.prompts["labels"]
        md += (f"> {lb['tokens']}: {tok_md:,} ({lb['vs_pdf']}: ≈{tok_pdf:,} con imágenes de página, {tok_txt:,} solo texto; "
               f"−{max(0, tokens['saving']):.0f} % / {'−' if tokens['saving_text'] >= 0 else '+'}{abs(tokens['saving_text']):.0f} %).\n")
        self._log(f"Tokens aprox.: resumen {tok_md:,} · PDF completo ≈{tok_pdf:,} (ahorro {tokens['saving']} %) · "
                  f"solo texto del PDF {tok_txt:,} ({tokens['saving_text']} %)")
        if tok_md > 0.8 * tok_txt:
            self._log("⚠ Documento muy denso (tablas y fórmulas): ningún resumen fiel es mucho más corto que su texto. "
                      "El ahorro real está en no enviar las imágenes de página. Usa «Compacto IA» o «Ultra-corto» para reducir más.")
        self.ui.stats(quality=quality, tokens=tokens)
        result = {
            "markdown": md, "title": title, "file": name, "pages": n_pages, "chunks": n,
            "language": self.lang_name, "vision_pages": vis_pages, "figures": len(figures),
            "quality": quality, "tokens": tokens, "elapsed": time.time() - t_start, "from_cache": False,
        }
        return self._publish(result, md, name, pdf_hash, phash)

    # ------------------------------------------------------- modo «Texto limpio»
    def _process_clean(self, name, pages, n_pages, vis_pages, title, t_start, pdf_hash, phash) -> dict:
        """Convierte el PDF en Markdown limpio y compacto SIN reescribir su contenido: cero alucinaciones.
        La IA solo escribe (opcionalmente) una descripción corta de 3-5 frases al principio."""
        self.ui.phase("map")
        self.ui.progress(0.40, "Limpiando y estructurando el texto...")
        for p in pages:   # la geometría del PDF confirma o desmiente las dudas de la visión antes del texto y del informe
            p.eq_flags = resolve_flags(p.eqs, p.eq_flags, p.geo)
        parts = [page_text_for_clean(p.text, p.vis, p.eqs, p.eq_flags, p.geo) for p in pages if p.text.strip()]
        body = clean_text("\n\n".join(parts))
        fid = self._fidelity(pages, body)
        hs = headings(body)
        self._log(f"Texto limpio: {len(body):,} caracteres · {len(hs)} títulos")

        tldr = ""
        try:
            self.ui.phase("reduce")
            self.ui.progress(0.75, "Redactando una descripción breve...")
            outline = "\n".join(hs[:60])
            tldr = self._ask(fill(self.prompts["tldr"], doc_title=self.doc_title, outline=outline, text=body[:5000],
                                  lang_name=self.lang_name), 260, "Descripción breve del documento")
            tldr, _ = scrub_text_numbers(tldr, "\n".join(p.raw + "\n" + p.text for p in pages))
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            self._log(f"⚠ No se pudo redactar la descripción breve: {exc}")

        lb = self.prompts["labels"]
        meta = (f"> {lb['file']}: {name} · {n_pages} {lb['pages']} · {lb['language']}: {self.lang_name} · "
                f"{lb['generated']}: {datetime.now():%Y-%m-%d %H:%M}")
        head = f"# {self.doc_title}\n\n{meta}\n\n"
        if tldr.strip():
            head += f"{lb['tldr']}: {' '.join(tldr.split())}\n\n---\n\n"
        md = nfc(compact_latex(convert_arrays(head + body))) + "\n"
        md, chain_errors = mark_numeric_errors(md)
        for err in chain_errors:
            fid.warn(0, f"cadena numérica incorrecta en el resumen final: {err}")
        report = fid.markdown()
        if report:
            md += "\n---\n\n" + report + "\n"
        self._log("Fidelidad: " + (fid.summary() or "sin fórmulas ni código que verificar"))
        source_text = "\n".join(p.raw + "\n" + p.text + "\n" + "\n".join(e for _, e in p.eqs) for p in pages)
        quality = audit_numbers(md, md, source_text)
        tokens = self._token_stats(pages, md, n_pages)
        tok_md, tok_pdf = tokens["summary"], tokens["pdf"]
        md += (f"\n---\n\n> {lb['audit']}: {quality['coverage']} % {lb['coverage']}. "
               f"{lb['tokens']}: {tok_md:,} ({lb['vs_pdf']}: ≈{tok_pdf:,} con imágenes de página).\n")
        self._log(f"Tokens aprox.: {tok_md:,} · PDF completo ≈{tok_pdf:,} (ahorro {tokens['saving']} %)")
        self.ui.stats(pages=n_pages, quality=quality, tokens=tokens, fidelity=fid.as_dict())
        result = {"markdown": md, "title": title, "file": name, "pages": n_pages, "chunks": 0,
                  "language": self.lang_name, "vision_pages": vis_pages, "figures": 0, "quality": quality,
                  "tokens": tokens, "fidelity": fid.as_dict(), "elapsed": time.time() - t_start, "from_cache": False}
        return self._publish(result, md, name, pdf_hash, phash)

    def _fidelity(self, pages, body: str) -> Fidelity:
        """Informe de fidelidad: fórmulas verificadas, bloques de código comprobados y avisos por página."""
        fid = Fidelity(double_read=bool(self.s.get("double_vision", True)))
        for p in pages:
            for k, (_, eq) in enumerate(p.eqs):
                fl: dict = p.eq_flags[k] if k < len(p.eq_flags) else {"ok": True, "why": []}
                fid.formulas_total += 1
                if fl.get("ok", True):
                    fid.formulas_ok += 1
                else:
                    fid.warn(p.number, f"fórmula dudosa «{eq[:70]}»: " + "; ".join(fl.get("why", [])))
            for w in p.warns:
                fid.warn(p.number, w)
        raw_all = "\n".join(p.raw or p.text for p in pages)
        for lang, code in fenced_blocks(body):
            fid.code_blocks += 1
            problems = check_code_block(code, lang, raw_all)
            if problems:
                fid.warn(0, f"bloque de código «{code.strip().splitlines()[0][:50]}»: " + "; ".join(problems))
            else:
                fid.code_ok += 1
        return fid

    # ------------------------------------------------------------- figuras
    def _describe_figures(self, pdf_path, pdf_hash, pages, chunks, vision) -> dict[int, str]:
        """Detecta páginas con gráficos (menciones + imágenes/vectores) y los describe con visión."""
        self.ui.phase("figures")
        by_num = {p.number: p for p in pages}
        wanted: dict[int, tuple[str, bool]] = {}
        doc0 = open_pdf(pdf_path)
        try:
            for pn, pg in by_num.items():
                page = doc0[pn - 1]
                area = page.rect.width * page.rect.height
                # Imágenes reales: ni el fondo de página (≈100 % del área) ni logos diminutos (< 2 %)
                real_imgs = [im for im in page.get_image_info()
                             if 0.02 <= ((im["bbox"][2] - im["bbox"][0]) * (im["bbox"][3] - im["bbox"][1])) / max(area, 1) <= 0.85]
                dr = page.get_drawings()
                # Un gráfico vectorial (superficie 3D, diagrama) está hecho de polígonos rellenos; las cajas y tablas son rectángulos
                polys = sum(1 for d in dr if d.get("fill") is not None and d["items"] and len(d["items"]) >= 3
                            and all(it[0] == "l" for it in d["items"]))
                curves = sum(1 for d in dr if any(it[0] in ("c", "qu") for it in d["items"]))
                keyword = bool(_FIGURE_RE.search(pg.raw or pg.text))
                if real_imgs or polys >= 10 or (keyword and curves >= 5):
                    wanted[pn] = ((pg.raw or pg.text)[-1500:], bool(real_imgs) or polys >= 10)
        finally:
            doc0.close()
        limit = int(self.cfg["extraction"]["max_figure_pages"])
        todo = sorted(wanted)[:limit]
        if len(wanted) > limit:
            self._log(f"Se detectaron {len(wanted)} páginas con gráficos; se describirán las primeras {limit}.")
        if not todo:
            self._log("No se detectaron gráficos que describir.")
            return {}

        fkey = cache.params_hash({"v": "4", "vm": self.cfg["ollama"]["vision_model"], "lang": self.lang_name,
                                  "dpi": self.cfg["extraction"]["figure_dpi"],
                                  "pr": cache.params_hash({"p": self.prompts["figure"], "n": self.prompts["figure_none"]})})
        saved: dict = cache.load(pdf_hash, "fig", fkey) or {}
        figures: dict[int, str] = {int(k): v for k, v in saved.items()}
        doc = open_pdf(pdf_path)
        try:
            for j, pn in enumerate(todo, 1):
                self._check()
                self.ui.progress(0.15 + 0.07 * (j - 1) / len(todo), f"Describiendo gráficos... página {pn} ({j}/{len(todo)})")
                if pn in figures:  # ya descrita (caché); "" = sin figuras
                    continue
                try:
                    img = render_page(doc, pn - 1, int(self.cfg["extraction"]["figure_dpi"]), pdf_hash)
                    t0 = time.time()
                    self._log(f"▶ Visión: describiendo gráfico de la página {pn} ({j}/{len(todo)})")
                    ctx, certain = wanted[pn]
                    desc = vision.describe_figure(img, pn, self.doc_title, ctx, allow_none=not certain)
                    no_fig = "SIN_FIGURAS" in desc or re.search(
                        r"(?i)\b(no\s+(?:contiene|hay|se\s+observan?|muestra|incluye)\s+(?:ning[uú]n[ao]?s?\s+)?"
                        r"(?:gr[aá]fic|diagrama|ilustraci|figura)|contains no (?:chart|diagram|figure))", desc[:400])
                    figures[pn] = "" if no_fig else self._check_quotes(desc, by_num[pn].raw)
                    self._log(f"✔ Página {pn} descrita en {fmt_time(time.time() - t0)}")
                    cache.save(pdf_hash, "fig", fkey, figures)
                except Cancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("No se pudo describir la página %d: %s", pn, exc)
                    self._log(f"⚠ No se pudo describir la página {pn}: {exc}")
        finally:
            doc.close()
        return {pn: figures[pn] for pn in todo if figures.get(pn)}

    @staticmethod
    def _check_quotes(desc: str, raw: str) -> str:
        """Los títulos entre comillas que la visión cita deben existir en el texto vectorial de la página;
        si no, se marcan como no verificados (la visión a veces inventa títulos)."""
        fold = lambda t: re.sub(r"\s+", " ", t.lower()).strip()   # noqa: E731
        raw_f = fold(raw)

        def rep(m):
            return m.group(0) if fold(m.group(1)) in raw_f else ""
        out = re.sub(r'["“«]([^"”»\n]{4,90})["”»]', rep, desc)
        return re.sub(r"\s{2,}", " ", re.sub(r"\s+([.,;:])", r"\1", out)).strip()

    @staticmethod
    def _figure_pages(ch: Chunk, figures: dict[int, str]) -> list[int]:
        """Páginas con gráfico que pertenecen a ESTE fragmento: el texto del fragmento en esa página (o en la
        anterior, donde suele estar el pie «Representación 3D») menciona un gráfico. Así un gráfico no se atribuye
        al problema vecino que comparte página."""
        portions: dict[int, str] = {}
        cur = ch.page_start
        for part in re.split(r"(⟦p\.\d+⟧)", ch.text):
            m = re.fullmatch(r"⟦p\.(\d+)⟧", part)
            if m:
                cur = int(m.group(1))
            else:
                portions[cur] = portions.get(cur, "") + part
        out = []
        for pn in range(ch.page_start, ch.page_end + 1):
            if pn not in figures:
                continue
            here = _FIGURE_RE.search(portions.get(pn, ""))
            before = pn - 1 in portions and pn in portions and _FIGURE_RE.search(portions[pn - 1])
            if here or before:
                out.append(pn)
        return out

    # ----------------------------------------------------------------- MAP
    def _map_phase(self, pdf_hash, phash, chunks: list[Chunk], figures: dict[int, str]) -> list[str]:
        n = len(chunks)
        mkey = cache.params_hash({"p": phash, "kind": "map"})
        saved: dict = (cache.load(pdf_hash, "map", mkey) or {}) if self.s["use_cache"] else {}
        map_detail = self.prompts["map_detail"][self.level_key]
        npred = int(self.level["map_tokens"])
        notes: list[str] = []
        for ch in chunks:
            self._check()
            self.ui.progress(0.22 + 0.30 * (ch.index - 1) / n, f"Resumiendo chunk {ch.index}/{n}...")
            key = str(ch.index)
            if key in saved:
                self._log(f"↺ Chunk {ch.index}/{n} recuperado de caché")
                notes.append(saved[key])
                continue
            figs = [f"[Página {pn}]\n{figures[pn]}" for pn in self._figure_pages(ch, figures)]
            figures_block = ""
            if figs:
                figures_block = fill(self.prompts["figures_block"], descriptions="\n\n".join(figs)) + "\n"
            formulas_block = ""
            if ch.eqs:
                formulas = "\n".join(f"- ${e}$" for e in ch.eqs)[:2800]
                formulas_block = fill(self.prompts["formulas_block"], formulas=formulas) + "\n"
            ratio = self.level.get("ratio")
            budget_line = ""
            if ratio:
                src_words = len(ch.text.split())
                budget_line = fill(self.prompts["budget_line"], max_words=max(25, int(src_words * float(ratio))),
                                   src_words=src_words) + "\n"
            prompt = fill(self.prompts["map"], doc_title=self.doc_title, section_title=ch.title or "—", i=ch.index, n=n,
                          pages=ch.pages, formulas_block=formulas_block, figures_block=figures_block, chunk=ch.text,
                          lang_name=self.lang_name, map_detail=map_detail + "\n" + budget_line)
            out = self._ask(prompt, npred, f"Resumiendo chunk {ch.index}/{n} (págs. {ch.pages})")
            notes.append(out)
            saved[key] = out
            cache.save(pdf_hash, "map", mkey, saved)
        return notes

    # --------------------------------------------------------- verificación
    def _verify_phase(self, pdf_hash, phash, chunks: list[Chunk], notes: list[str]) -> list[str]:
        """Audita cada nota contra el texto de SU fragmento. Solo acepta la versión corregida si no empeora."""
        self.ui.phase("verify")
        n = len(chunks)
        rcfg = self.cfg["reduce"]
        keep = float(rcfg["min_keep_ratio"])
        passes = max(1, min(2, int(self.s["refine_iterations"])))
        vkey = cache.params_hash({"p": phash, "kind": "ver"})
        saved: dict = (cache.load(pdf_hash, "ver", vkey) or {}) if self.s["use_cache"] else {}
        out: list[str] = []
        fixed_total = 0
        for ch, note in zip(chunks, notes, strict=True):
            self._check()
            self.ui.progress(0.52 + 0.38 * (ch.index - 1) / n, f"Verificando fragmento {ch.index}/{n} contra la fuente...")
            key = str(ch.index)
            if key in saved:
                self._log(f"↺ Verificación {ch.index}/{n} recuperada de caché")
                out.append(saved[key])
                continue
            source = ch.text + ("\n\n" + "\n".join(ch.eqs) if ch.eqs else "")
            cur = note
            for it in range(passes):
                nums, terms = unsupported_numbers(cur, source), unknown_terms(cur, source)
                if it > 0 and not nums and not terms:
                    break  # ya no hay avisos automáticos: una segunda pasada solo gastaría tiempo
                hint = ""
                if nums:
                    hint += fill(self.prompts["verify_hint"], numbers=", ".join(nums[:25])) + "\n"
                if terms:
                    hint += fill(self.prompts["verify_hint_terms"], terms=", ".join(terms[:15])) + "\n"
                if nums or terms:
                    self._log(f"⚠ Fragmento {ch.index}: sin respaldo en la fuente → cifras: {', '.join(nums[:10]) or '—'}"
                              f" · términos: {', '.join(terms[:8]) or '—'}")
                fixed = self._ask(
                    fill(self.prompts["verify"], pages=ch.pages, doc_title=self.doc_title, source=source, summary=cur,
                         hint=hint, lang_name=self.lang_name),
                    int(self.level["map_tokens"]) + 400,
                    f"Verificando fragmento {ch.index}/{n} (págs. {ch.pages}, pasada {it + 1}/{passes})")
                new_bad = len(unsupported_numbers(fixed, source)) + len(unknown_terms(fixed, source))
                if keep * len(cur) <= len(fixed) <= 1.6 * len(cur) and new_bad <= len(nums) + len(terms):
                    if fixed != cur:
                        fixed_total += 1
                    self._log(f"   → avisos automáticos: {len(nums) + len(terms)} antes, {new_bad} después")
                    cur = fixed
                else:
                    self._log("   → versión auditada descartada (cambió demasiado o empeoró); se conserva la anterior")
                    break
            out.append(cur)
            saved[key] = cur
            cache.save(pdf_hash, "ver", vkey, saved)
        self._log(f"Verificación terminada: {fixed_total} de {n} fragmentos corregidos")
        return out

    # ------------------------------------------------- saneado determinista
    def _scrub_phase(self, chunks: list[Chunk], notes: list[str]) -> list[str]:
        """Comprobaciones SIN IA contra la fuente: fórmulas, cifras, numeración inventada, relleno y tablas omitidas."""
        out: list[str] = []
        totals: dict[str, int] = {}
        for ch, note in zip(chunks, notes, strict=True):
            source = ch.text + ("\n\n" + "\n".join(ch.eqs) if ch.eqs else "")
            clean, st = scrub_note(note, source, ch.eqs)
            for k, v in st.items():
                totals[k] = totals.get(k, 0) + v
            out.append(clean)
        self._log("Saneado determinista: " + " · ".join(f"{k}: {v}" for k, v in totals.items()) +
                  "  (formulas = corregidas con la imagen, foreign = fórmulas inventadas eliminadas, numbers = líneas con "
                  "cifras sin respaldo eliminadas, filler/numbering = relleno y numeración inventada, tables = tablas omitidas añadidas)")
        return out

    # ------------------------------------------------------------- síntesis
    @staticmethod
    def _fit_notes(texts: list[str], budget: int) -> str:
        """Une notas recortando proporcionalmente para caber en 'budget' caracteres."""
        joined = "\n\n".join(texts)
        if len(joined) <= budget:
            return joined
        per = max(200, budget // max(1, len(texts)))
        return "\n\n".join(t[:per] for t in texts)

    def _synthesize(self, chunks: list[Chunk], notes: list[str], n_pages: int, source_text: str = "",
                    figures: dict | None = None) -> str:
        figures = figures or {}
        """Resumen ejecutivo + cuerpo (notas verificadas, sin reescribir) + conclusiones generales."""
        lb = self.prompts["labels"]
        rcfg = self.cfg["reduce"]
        iters = int(self.s["refine_iterations"])
        # Notas comprimidas para el resumen ejecutivo: solo ideas principales y conclusiones de cada fragmento
        brief = []
        for ch, note in zip(chunks, notes, strict=True):
            txt = section_text(note, ("Ideas principales", "Main ideas", "Conclusiones", "Conclusions"))
            if txt:
                brief.append(f"[{lb['pp']} {ch.pages}] {ch.title}\n{txt}".strip())
        brief_txt = self._fit_notes(brief, int(rcfg["batch_chars"]))
        common = dict(doc_title=self.doc_title, lang_name=self.lang_name, final_detail="", n_chunks=len(chunks),
                      n_pages=n_pages)
        self.ui.phase("reduce")
        self.ui.progress(0.90, "Redactando resumen ejecutivo y conclusiones...")
        overview = self._ask(fill(self.prompts["overview"], notes=brief_txt, **common), 1500,
                             "Resumen ejecutivo y conclusiones generales")

        def on_iter(j):
            self.ui.phase("refine")
            self.ui.progress(0.93 + 0.06 * j / max(1, iters), f"Refinando resumen ejecutivo ({j + 1}/{iters})...")

        overview = self._self_refine(overview, brief_txt, iters, common, 1500, "resumen ejecutivo", on_iter, max_growth=1.4)
        overview = self._sanitize_overview(overview)
        if source_text:
            overview, dropped = scrub_text_numbers(overview, source_text)
            if dropped:
                self._log(f"Saneado del resumen ejecutivo: {dropped} línea(s) con cifras sin respaldo eliminadas")

        # Estructura fija: Título → Resumen ejecutivo → Contenido principal → Problemas → Fórmulas clave →
        # Conclusiones → Figuras y tablas. Se reorganizan las notas por tipo de sección (sin reescribirlas).
        sections = []
        last_title = ""
        for ch, note in zip(chunks, notes, strict=True):
            title, cont = ch.title, ch.cont
            if not title:
                # Fragmento sin encabezado propio: continúa la sección anterior (o es el inicio del documento)
                title, cont = (last_title, True) if last_title else (self.doc_title, False)
            last_title = title
            heading = f"{title} ({lb['cont']}, {lb['pp']} {ch.pages})" if cont else f"{title} ({lb['pp']} {ch.pages})"
            sections.append((heading, parse_note(note)))

        seen_global: set[str] = set()      # una línea ya escrita en cualquier apartado no se repite (ahorra tokens)
        cite_re = re.compile(r"\s*\((?:p|pp|pág|págs|pags|page|pages)\.?\s*\d+(?:\s*[-–]\s*\d+)?\)", re.I)

        def block(keys: tuple[str, ...], mode: str) -> str:
            """mode: 'prose' = deduplica texto largo entre apartados; 'local' = deduplica solo dentro del apartado;
            'none' = no deduplica (enunciados: se repiten legítimamente entre problemas)."""
            out = []
            seen_local: set[str] = set()
            for heading, sec in sections:
                body = "\n\n".join(sec[k] for k in keys if k in sec)
                body = cite_re.sub("", body)   # el encabezado de la sección ya indica las páginas
                if mode == "prose":
                    body = unique_lines(body, seen_global, min_len=70, prose_only=True)
                elif mode == "local":
                    body = unique_lines(body, seen_local)
                elif mode == "section":      # el solape entre fragmentos repite los enunciados dentro de una misma sección
                    body = unique_lines(body, set(), min_len=3)
                if body.strip():
                    out.append(f"### {heading}\n\n{body.strip()}")
            return "\n\n".join(out)

        m = re.search(r"^##\s+(conclusiones generales|general conclusions).*$", overview, re.I | re.M)
        head, concl = (overview[:m.start()].rstrip(), overview[m.start():].strip()) if m else (overview.strip(), "")
        concl = re.sub(r"^##\s+.*\n?", "", concl, count=1).strip()
        parts = [head]
        main_keys = ("data",) if self.level_key == "ia" else ("ideas", "data")
        if self.level_key == "ia":      # sin datos propios, la idea principal de la sección sí se conserva
            for _, sec in sections:
                if "data" not in sec and "ideas" in sec:
                    sec["data"] = sec["ideas"]
        for title, text in ((lb["main"], block(main_keys, "prose")),
                            (lb["problems"], block(("statements",), "section")),
                            (lb["formulas"], block(("formulas",), "local"))):
            if text:
                parts.append(f"## {title}\n\n{text}")
        by_section = "" if self.level_key == "ia" else block(("conclusions",), "prose")
        concl_md = concl + (f"\n\n### {lb['by_section']}\n\n{by_section}" if by_section else "")
        if concl_md.strip():
            parts.append(f"## {lb['conclusions']}\n\n{concl_md.strip()}")
        figs_text = "" if self.level_key == "ia" else block(("figures",), "prose")
        if figures:
            clip = (lambda t: t[:260].rsplit(" ", 1)[0] + "…") if self.level_key == "ia" else (lambda t: t)
            figs_text += ("\n\n" if figs_text else "") + f"### {lb['vision_figs']}\n\n" + "\n\n".join(
                f"**{lb['page']} {pn}.** {clip(' '.join(figures[pn].split()))}" for pn in sorted(figures))
        if figs_text.strip():
            parts.append(f"## {lb['figures']}\n\n{figs_text.strip()}")
        # Presupuesto de tokens: en «Compacto IA» y «Normal» el resumen debe ser claramente más corto que el texto del PDF.
        target = {"ia": 0.5, "normal": 0.85}.get(self.level_key)
        src_tokens = getattr(self, "src_tokens", 0)
        if target and src_tokens:
            limit = target * src_tokens
            tot = lambda: est_tokens("\n\n".join(parts))      # noqa: E731
            if tot() > limit:       # 1.º se descartan los gráficos descritos con visión
                parts = [p for p in parts if not p.startswith(f"## {lb['figures']}")]
            if tot() > limit:       # 2.º las conclusiones por sección (quedan las generales)
                parts = [re.sub(r"\n\n### " + re.escape(lb["by_section"]) + r".*", "", p, flags=re.S)
                         if p.startswith(f"## {lb['conclusions']}") else p for p in parts]
            if tot() > limit:
                self._log(f"⚠ El resumen (≈{tot():,} tokens) supera el objetivo del nivel ({int(limit):,}); "
                          "el documento es muy denso en datos.")
        return "\n\n".join(parts)

    @staticmethod
    def _sanitize_overview(text: str) -> str:
        """El resumen ejecutivo solo puede tener: título, resumen ejecutivo y conclusiones generales. Cualquier otro
        apartado (p. ej. «Notas originales», eco de las notas) se elimina."""
        keep_re = re.compile(r"^#{1,2}\s+(?:resumen ejecutivo|executive summary|conclusiones generales|general conclusions)\b", re.I)
        out: list[str] = []
        keep = True
        for ln in text.splitlines():
            if re.match(r"^#{1,6}\s", ln):
                is_title = ln.startswith("# ") and not out
                keep = is_title or bool(keep_re.match(ln))
            if keep:
                out.append(ln)
        return "\n".join(out).strip()

    def _self_refine(self, summary: str, notes_txt: str, iters: int, common: dict, tokens: int,
                     label: str, on_iter, max_growth: float | None = None) -> str:
        """Critica el texto frente a las notas y lo mejora; repite 'iters' veces."""
        excerpt_budget = int(self.cfg["reduce"]["critique_notes_chars"])
        notes_excerpt = notes_txt if len(notes_txt) <= excerpt_budget else self._fit_notes([notes_txt], excerpt_budget)
        keep = float(self.cfg["reduce"]["min_keep_ratio"])
        for j in range(iters):
            self._check()
            on_iter(j)
            critique = self._ask(
                fill(self.prompts["critique"], notes=notes_excerpt, summary=summary, **common), 900,
                f"Criticando {label} (iteración {j + 1}/{iters})", clean=False)
            improved = self._ask(
                fill(self.prompts["refine"], notes=notes_excerpt, critique=critique, summary=summary, **common),
                tokens, f"Mejorando {label} (iteración {j + 1}/{iters})")
            if self.level_key != "ultra" and len(improved) < keep * len(summary):
                self._log(f"⚠ La versión refinada era mucho más corta ({len(improved)} vs {len(summary)}); se conserva la anterior.")
                continue
            if max_growth and len(improved) > max_growth * len(summary):
                self._log(f"⚠ La versión refinada creció demasiado ({len(improved)} vs {len(summary)}; "
                          "suele ser eco de las notas); se conserva la anterior.")
                continue
            summary = improved
        return summary

    # ------------------------------------------------------- ruta Ultra-corto
    def _group(self, items: list[tuple[str, str]], budget: int) -> list[list[tuple[str, str]]]:
        groups: list[list[tuple[str, str]]] = []
        cur: list[tuple[str, str]] = []
        size = 0
        for it in items:
            if cur and size + len(it[1]) > budget:
                groups.append(cur)
                cur, size = [], 0
            cur.append(it)
            size += len(it[1])
        if cur:
            groups.append(cur)
        return groups

    @staticmethod
    def _pages_span(group) -> str:
        nums = []
        for pages, _ in group:
            nums += [int(x) for x in re.findall(r"\d+", pages)]
        if not nums:
            return "?"
        return str(min(nums)) if min(nums) == max(nums) else f"{min(nums)}-{max(nums)}"

    @staticmethod
    def _join_group(group) -> str:
        return "\n\n".join(f"#### (p. {pages})\n{text}" for pages, text in group)

    def _ultra(self, chunks: list[Chunk], notes: list[str], n_pages: int) -> str:
        """Map-Reduce clásico: fusión jerárquica de notas + consolidación + Self-Refine."""
        budget = int(self.cfg["reduce"]["batch_chars"])
        iters = int(self.s["refine_iterations"])
        final_detail = self.prompts["final_detail"][self.level_key]
        ftok = int(self.level["final_tokens"])
        common = dict(doc_title=self.doc_title, lang_name=self.lang_name, final_detail=final_detail,
                      n_chunks=len(chunks), n_pages=n_pages)
        groups = self._group([(ch.pages, note) for ch, note in zip(chunks, notes, strict=True)], budget)
        level = 0
        self.ui.phase("reduce")
        while len(groups) > 1 and level < 6:
            level += 1
            self._log(f"Reduce nivel {level}: fusionando {len(groups)} lotes")
            merged = []
            for k, g in enumerate(groups, 1):
                self.ui.progress(0.55 + 0.10 * (k - 1) / len(groups), f"Fusionando notas (nivel {level}) lote {k}/{len(groups)}...")
                txt = self._ask(fill(self.prompts["merge_notes"], notes=self._join_group(g), **common),
                                2200, f"Fusionando lote {k}/{len(groups)}")
                merged.append((self._pages_span(g), txt))
            new_groups = self._group(merged, budget)
            if len(new_groups) >= len(groups):  # no converge: forzar
                new_groups = [merged]
            groups = new_groups

        notes_txt = self._join_group(groups[0])
        self.ui.progress(0.70, "Generando resumen consolidado...")
        summary = self._ask(fill(self.prompts["consolidate"], notes=notes_txt, **common), ftok,
                            "Generando resumen consolidado")

        def on_iter(j):
            self.ui.phase("refine")
            self.ui.progress(0.75 + 0.23 * j / max(1, iters), f"Refinando... iteración {j + 1}/{iters}")

        return self._self_refine(summary, notes_txt, iters, common, ftok, "resumen final", on_iter)

    # ------------------------------------------------------------ ensamblado
    def _assemble(self, name, n_pages, vis_pages, summary, figures, source_text):
        lb = self.prompts["labels"]
        meta = (f"> **{lb['file']}:** {name} · {n_pages} {lb['pages']} · **{lb['language']}:** {self.lang_name} · "
                f"**{lb['level']}:** {self.s['detail']} · {self.text_model} · "
                f"{lb['generated']}: {datetime.now():%Y-%m-%d %H:%M}")
        if vis_pages:
            meta += f" · {len(vis_pages)} {lb['ocr_pages']}"
        summary = summary.strip()
        # Inserta la línea de metadatos tras el primer título si existe
        m = re.match(r"(#\s+.+\n)", summary)
        body = f"{m.group(1)}\n{meta}\n\n{summary[m.end():].lstrip()}" if m else f"# {self.doc_title}\n\n{meta}\n\n{summary}"
        md = body
        if figures and self.level_key == "ultra":     # en los demás niveles los gráficos ya van en «Figuras y tablas»
            md += f"\n\n---\n\n## {lb['annex']}\n"
            for pn in sorted(figures):
                md += f"\n### {lb['page']} {pn}\n\n{figures[pn].strip()}\n"
        md = nfc(compact_latex(convert_arrays(md)))
        md, chain_errors = mark_numeric_errors(md)
        if chain_errors:
            self._log(f"⚠ {len(chain_errors)} fórmula(s) del resumen con cadena numérica incorrecta (marcadas con ⚠): "
                      + "; ".join(chain_errors[:3]))
        if self.level_key == "ia":
            md = re.sub(r"\*\*(.+?)\*\*", r"\1", md)
        quality = audit_numbers(body, md, source_text)
        unsup = quality["unsupported"]
        md += (f"\n---\n\n> {lb['audit']}: {quality['coverage']} % {lb['coverage']}; "
               f"{len(unsup)} {lb['unsupported']}" + (f" ({', '.join(unsup[:15])})" if unsup else "") + ".\n")
        return md, quality
