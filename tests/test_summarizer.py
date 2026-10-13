"""Pruebas del orquestador de resúmenes (Summarizer) con el LLM y la visión simulados: sin Ollama, sin GPU."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf
import yaml

from local_ai_studio.core import cache
from local_ai_studio.core.errors import Cancelled, PDFError
from local_ai_studio.pdf import summarizer
from local_ai_studio.pdf.summarizer import UI, Summarizer

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))

PAGES = [
    "Informe de prueba sobre integración numérica\n\nProblema 1. Se mide la temperatura en una sala de servidores. "
    "La regla de Simpson 1/3 aproxima la integral con parábolas. El resultado obtenido fue 1080 grados por metro cuadrado.",
    "Problema 2. La complejidad ciclomática del grafo se calcula como E - N + 2P. Para el ejemplo vale 7 y el umbral es 10. "
    "El equipo revisó 25 funciones y encontró 3 por encima del límite.",
]
CLEAN = {"detail": "Texto limpio", "language": "auto", "chunk_size": 1800, "chunk_overlap": 200, "refine_iterations": 0,
         "use_cache": True, "describe_figures": False, "math_vision": False, "double_vision": False, "verify": False}


def make_pdf(tmp: Path, pages=PAGES, name="informe.pdf") -> Path:
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_textbox(pymupdf.Rect(50, 60, 540, 700), text, fontsize=11)
    path = tmp / name
    doc.save(str(path))
    doc.close()
    return path


class RecUI(UI):
    def __init__(self):
        self.logs, self.phases, self.fracs = [], [], []

    def log(self, msg):
        self.logs.append(msg)

    def phase(self, key):
        self.phases.append(key)

    def progress(self, frac, msg):
        self.fracs.append(frac)


class FakeVision:
    def __init__(self, *args, **kwargs):
        pass

    def transcribe_page(self, img, n):
        return ""


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out_dir = self.tmp / "resumenes"
        self.cache_dir = self.tmp / "cache"
        self.out_dir.mkdir()
        self.cache_dir.mkdir()
        self.calls: list[str] = []
        for p in (mock.patch.object(summarizer, "SUMMARIES_DIR", self.out_dir),
                  mock.patch.object(cache, "CACHE_DIR", self.cache_dir),
                  mock.patch.object(summarizer, "check_ollama", lambda *a, **k: None),
                  mock.patch.object(summarizer, "Vision", FakeVision),
                  mock.patch.object(summarizer, "chat_stream", self.fake_llm)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self.pdf = make_pdf(self.tmp)

    def fake_llm(self, cfg, model, messages, options, cancel=None, on_text=None):
        """LLM de mentira: cada petición se registra y la respuesta es corta y fija."""
        user = messages[-1]["content"]
        self.calls.append(user[:60])
        return "Documento de prueba sobre integración numérica y complejidad de código."

    def run_summary(self, **settings):
        ui = RecUI()
        res = Summarizer(CFG, {**CLEAN, **settings}, ui, threading.Event()).process(str(self.pdf))
        return res, ui


class TestTextoLimpio(Base):
    def test_genera_markdown_con_el_contenido_del_pdf_sin_reescribirlo(self):
        res, ui = self.run_summary()
        md = res["markdown"]
        for fragment in ("Problema 1", "Problema 2", "1080", "E - N + 2P", "25 funciones"):
            self.assertIn(fragment, md)
        self.assertEqual((res["pages"], res["chunks"], res["from_cache"]), (2, 0, False))
        self.assertEqual(res["language"], "español")
        self.assertIn("done", ui.phases)
        self.assertEqual(ui.fracs[-1], 1.0)

    def test_guarda_el_markdown_y_lo_deja_en_el_resultado(self):
        res, _ = self.run_summary()
        saved = Path(res["saved_md"])
        self.assertEqual(saved.parent, self.out_dir)
        self.assertEqual(saved.name, "informe_resumen.md")
        self.assertEqual(saved.read_text(encoding="utf-8"), res["markdown"])

    def test_la_unica_llamada_al_modelo_es_la_descripcion_breve(self):
        self.run_summary()
        self.assertEqual(len(self.calls), 1)

    def test_si_el_modelo_falla_el_texto_limpio_se_entrega_igual(self):
        with mock.patch.object(summarizer, "chat_stream", side_effect=RuntimeError("Ollama caído")):
            res, ui = self.run_summary()
        self.assertIn("Problema 1", res["markdown"])
        self.assertTrue(any("No se pudo redactar la descripción breve" in m for m in ui.logs))

    def test_la_segunda_vez_sale_de_la_cache_y_no_llama_al_modelo(self):
        self.run_summary()
        self.calls.clear()
        res, ui = self.run_summary()
        self.assertTrue(res["from_cache"])
        self.assertEqual(self.calls, [])
        self.assertTrue(any("caché" in m for m in ui.logs))

    def test_cambiar_un_ajuste_o_desactivar_la_cache_regenera(self):
        self.run_summary()
        self.calls.clear()
        res, _ = self.run_summary(use_cache=False)
        self.assertFalse(res["from_cache"])
        self.assertEqual(len(self.calls), 1)
        res2, _ = self.run_summary(language="en")                 # otro idioma de salida = otros ajustes = otra entrada de caché
        self.assertFalse(res2["from_cache"])

    def test_estadisticas_de_tokens_coherentes(self):
        res, _ = self.run_summary()
        t = res["tokens"]
        self.assertGreater(t["summary"], 0)
        self.assertEqual(t["pdf"], t["pdf_text"] + 2 * CFG["tokens"]["pdf_page_overhead"])
        self.assertGreater(t["saving"], 0)
        self.assertIn("quality", res)
        self.assertGreaterEqual(res["quality"]["coverage"], 90)    # el texto limpio conserva (casi) todas las cifras


class TestErrores(Base):
    def run_with(self, path, cfg=CFG):
        return Summarizer(cfg, dict(CLEAN), RecUI(), threading.Event()).process(str(path))

    def test_archivo_inexistente(self):
        with self.assertRaisesRegex(PDFError, "no existe"):
            self.run_with(self.tmp / "no_hay.pdf")

    def test_pdf_corrupto(self):
        bad = self.tmp / "roto.pdf"
        bad.write_bytes(b"esto no es un pdf")
        with self.assertRaises(PDFError):
            self.run_with(bad)

    def test_limite_de_paginas(self):
        cfg = {**CFG, "limits": {"max_pdf_mb": 200, "max_pages": 1}}
        with self.assertRaisesRegex(PDFError, "2 páginas"):
            self.run_with(self.pdf, cfg)

    def test_limite_de_tamano(self):
        cfg = {**CFG, "limits": {"max_pdf_mb": 0.00001, "max_pages": 800}}
        with self.assertRaisesRegex(PDFError, "pesa"):
            self.run_with(self.pdf, cfg)

    def test_pdf_sin_texto(self):
        blank = self.tmp / "blanco.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(str(blank))
        doc.close()
        with self.assertRaisesRegex(PDFError, "No se pudo extraer texto"):
            self.run_with(blank)

    def test_cancelar_no_deja_resultados_a_medias(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            Summarizer(CFG, dict(CLEAN), RecUI(), cancel).process(str(self.pdf))
        self.assertEqual(list(self.out_dir.iterdir()), [])
        self.assertFalse(any(self.cache_dir.glob("*_final_*.json")))


class TestNiveles(Base):
    """Los niveles con IA (map, síntesis, verificación) recorren todo el pipeline con un modelo simulado."""

    def test_nivel_normal_completa_y_registra_las_fases(self):
        res, ui = self.run_summary(detail="Normal", chunk_size=400, chunk_overlap=50, refine_iterations=1, verify=True)
        self.assertGreater(res["chunks"], 0)
        self.assertGreater(len(self.calls), res["chunks"])        # al menos una nota por fragmento, más la síntesis
        self.assertIn("map", ui.phases)
        self.assertIn("done", ui.phases)
        self.assertTrue(Path(res["saved_md"]).is_file())
        md = res["markdown"]
        self.assertIn("**Nivel:** Normal", md)
        self.assertIn("Documento de prueba sobre integración numérica", md)     # el cuerpo lo forman las notas del modelo
        self.assertIn("Auditoría automática", md)                                # y siempre se audita contra la fuente

    def test_nivel_compacto_ia(self):
        res, _ = self.run_summary(detail="Compacto IA", chunk_size=400, chunk_overlap=50)
        self.assertGreater(res["chunks"], 0)
        self.assertTrue(res["markdown"].strip())

    def test_ultra_corto_usa_la_ruta_map_reduce(self):
        res, ui = self.run_summary(detail="Ultra-corto", chunk_size=400, chunk_overlap=50)
        self.assertIn("map", ui.phases)
        self.assertTrue(res["markdown"].strip())

    def test_cancelar_en_mitad_de_un_nivel_con_ia(self):
        cancel = threading.Event()
        n = {"calls": 0}

        def llm(cfg, model, messages, options, c=None, on_text=None):
            n["calls"] += 1
            cancel.set()                                          # el usuario pulsa «Cancelar» tras la primera respuesta
            return "nota"

        with mock.patch.object(summarizer, "chat_stream", llm), self.assertRaises(Cancelled):
            Summarizer(CFG, {**CLEAN, "detail": "Normal", "chunk_size": 400, "chunk_overlap": 50}, RecUI(), cancel).process(str(self.pdf))
        self.assertEqual(n["calls"], 1)
        self.assertEqual(list(self.out_dir.iterdir()), [])


class TestUtilidades(Base):
    def test_clave_de_extraccion_cambia_con_la_version_y_los_ajustes(self):
        a = summarizer.extraction_key(CFG, True, True)
        self.assertEqual(a, summarizer.extraction_key(CFG, True, True))
        self.assertNotEqual(a, summarizer.extraction_key(CFG, False, True))
        self.assertNotEqual(a, summarizer.extraction_key(CFG, True, False))
        with mock.patch.object(summarizer, "EXTRACT_VERSION", "999"):
            self.assertNotEqual(a, summarizer.extraction_key(CFG, True, True))


if __name__ == "__main__":
    unittest.main()
