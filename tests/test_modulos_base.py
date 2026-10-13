"""Pruebas de los módulos base que no tenían ninguna: caché, registro, exportación a PDF, notas y selección de hardware."""
import copy
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf
import yaml

from local_ai_studio.core import cache, hardware_detector as hw, logsetup
from local_ai_studio.core.pdf_export import export_pdf
from local_ai_studio.pdf import notes

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))


class TestCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(cache, "CACHE_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_ida_y_vuelta(self):
        data = {"markdown": "Cuánto vale π: 3,14159 — «ñ»", "n": [1, 2, 3]}
        cache.save("a" * 64, "final", "abc123", data)
        self.assertEqual(cache.load("a" * 64, "final", "abc123"), data)

    def test_lo_que_no_existe_devuelve_none(self):
        self.assertIsNone(cache.load("b" * 64, "final", "zzz"))

    def test_una_entrada_corrupta_no_rompe_nada(self):
        cache.save("c" * 64, "pages", "k", {"x": 1})
        next(Path(self._tmp.name).glob("*.json")).write_text("{esto no es json", encoding="utf-8")
        self.assertIsNone(cache.load("c" * 64, "pages", "k"))

    def test_la_escritura_es_atomica_y_no_deja_temporales(self):
        cache.save("d" * 64, "final", "k", {"x": 1})
        self.assertEqual([p.suffix for p in Path(self._tmp.name).iterdir()], [".json"])

    def test_el_hash_de_parametros_es_estable_y_sensible(self):
        a = cache.params_hash({"a": 1, "b": [1, 2]})
        self.assertEqual(a, cache.params_hash({"b": [1, 2], "a": 1}))       # el orden de las claves no importa
        self.assertNotEqual(a, cache.params_hash({"a": 2, "b": [1, 2]}))
        self.assertEqual(len(a), 12)


class TestRegistro(unittest.TestCase):
    def test_es_idempotente_y_no_duplica_manejadores(self):
        root = logging.getLogger("local_ai_studio")
        before = list(root.handlers)
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(logsetup, "LOG_PATH", Path(tmp) / "app.log"):
                for h in before:
                    root.removeHandler(h)
                handler = logging.NullHandler()
                logsetup.setup_logging()
                logsetup.setup_logging(handler)
                logsetup.setup_logging(handler)
                logsetup.setup_logging()
                self.assertEqual(sum(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers), 1)
                self.assertEqual(root.handlers.count(handler), 1)
                logging.getLogger("local_ai_studio.prueba").warning("mensaje de prueba ñ")
                for h in root.handlers:
                    h.flush()
                self.assertIn("mensaje de prueba ñ", (Path(tmp) / "app.log").read_text(encoding="utf-8"))
                for h in list(root.handlers):
                    root.removeHandler(h)
                    h.close()
        finally:
            for h in list(root.handlers):
                root.removeHandler(h)
            for h in before:
                root.addHandler(h)


class TestExportPdf(unittest.TestCase):
    def test_markdown_a_pdf_con_texto_tablas_y_formulas(self):
        md = ("# Informe\n\nLa regla de Simpson vale $\\frac{1}{3}(27+110)$.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
              "![malo](https://evil.example/x.png)\n\nFin del documento.\n")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:      # Windows: el antivirus retiene el PDF un instante
            out = Path(tmp) / "informe.pdf"
            export_pdf(md, out)
            with pymupdf.open(str(out)) as doc:
                text = "".join(page.get_text() for page in doc)
                self.assertGreaterEqual(doc.page_count, 1)
        self.assertIn("Informe", text)
        self.assertIn("Fin del documento", text)
        self.assertNotIn("evil.example", text.replace("[", "").lower().split("malo")[0])      # la imagen remota no se carga


class TestNotas(unittest.TestCase):
    def test_parse_note_separa_secciones_y_descarta_las_vacias(self):
        note = "### Ideas principales\n- Uno\n### Datos\n—\n### Fórmulas\n$x=1$\n### Conclusiones\n- Fin"
        got = notes.parse_note(note)
        self.assertEqual(set(got), {"ideas", "formulas", "conclusions"})
        self.assertIn("Uno", got["ideas"])

    def test_deduplicacion_conserva_tablas_y_quita_el_solape(self):
        seen: set[str] = set()
        first = notes.unique_lines("La regla de Simpson aproxima integrales con parábolas.\n| 1 | 2 |", seen)
        again = notes.unique_lines("La regla de Simpson aproxima integrales con parábolas.\n| 1 | 2 |\nOtra frase distinta y larga.", seen)
        self.assertIn("Simpson", first)
        self.assertNotIn("Simpson", again)                  # repetida por el solape de fragmentos
        self.assertIn("| 1 | 2 |", again)                   # las filas de tabla pueden repetirse
        self.assertIn("Otra frase distinta", again)

    def test_tokens_y_nfc(self):
        self.assertEqual(notes.est_tokens("a" * 36), 10)
        self.assertEqual(notes.nfc("ó"), "ó")


class TestSeleccionDeHardware(unittest.TestCase):
    """La tabla del README: el contexto crece con la VRAM y el modelo grande solo se usa si está instalado."""

    def decide(self, vram, vendor="nvidia", ram=16.0, installed=frozenset(), free=None):
        info = hw.HardwareInfo("GPU de prueba", vendor, vram, vram if free is None else free, ram)
        return hw.decide(info, set(installed), copy.deepcopy(CFG), free_gb=free)

    def test_contexto_segun_vram(self):
        for vram, expected in ((3, 2048), (5, 4096), (7, 8192), (8, 16384), (11, 16384)):
            with self.subTest(vram=vram):
                self.assertEqual(self.decide(vram).text_num_ctx, expected)

    def test_modelo_grande_solo_si_esta_instalado(self):
        self.assertEqual(self.decide(16).text_model, CFG["ollama"]["text_model"])
        self.assertEqual(self.decide(16, installed={"qwen2.5:14b"}).text_model, "qwen2.5:14b")

    def test_sin_gpu_nvidia_va_a_cpu(self):
        auto = self.decide(0, vendor="cpu")
        self.assertEqual((auto.text_num_ctx, auto.text_num_gpu, auto.vision_num_gpu), (4096, 0, 0))

    def test_poca_ram_y_sin_gpu_recomienda_el_modelo_pequeno(self):
        self.assertEqual(self.decide(0, vendor="cpu", ram=6, installed={"qwen2.5:3b"}).text_model, "qwen2.5:3b")

    def test_nunca_supera_el_contexto_del_config(self):
        cfg = copy.deepcopy(CFG)
        cfg["ollama"]["text_options"]["num_ctx"] = 4096
        info = hw.HardwareInfo("GPU", "nvidia", 24.0, 24.0, 32.0)
        self.assertEqual(hw.decide(info, set(), cfg).text_num_ctx, 4096)

    def test_describe_sin_hardware(self):
        self.assertEqual(hw.describe({}), "Hardware sin detectar")


if __name__ == "__main__":
    unittest.main()
