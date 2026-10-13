"""Pruebas de las correcciones de la auditoría: cada una fija un defecto real que se encontró y se arregló."""
import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf

from local_ai_studio import __main__ as cli
from local_ai_studio.chat import engine as eng
from local_ai_studio.chat.store import DEFAULT_MODEL, DEFAULT_PREFS, PREFS_VERSION, load_prefs, save_prefs
from local_ai_studio.core import config, ollama_client
from local_ai_studio.core.errors import Cancelled
from local_ai_studio.core.textutil import detect_language, safe_name, sha256_file
from local_ai_studio.core import latex_text
from local_ai_studio.pdf import extractor
from local_ai_studio.rag.index import DocIndex


# ---------------------------------------------------------------------------------------------- textproc
class TestFormulasTruncadas(unittest.TestCase):
    """Una fórmula que termina en «\\» (salida del modelo cortada) hacía fallar la conversión con AttributeError."""

    def test_barra_invertida_final_no_rompe(self):
        for md in ("$x = 1\\$", "$\\frac{1}{2}\\$", "$\\sqrt\\$", "$a \\$"):
            with self.subTest(md=md):
                self.assertIsInstance(latex_text.latex_to_text(md), str)

    def test_las_formulas_normales_siguen_igual(self):
        self.assertEqual(latex_text.latex_to_text("$\\frac{1}{3}(27+110)$"), "1/3(27+110)")
        self.assertEqual(latex_text.latex_to_text("$x^{2}$"), "x²")


# ---------------------------------------------------------------------------------------------- red local
class TestClienteOllama(unittest.TestCase):
    def test_localhost_se_convierte_en_la_ip_de_loopback(self):
        f = ollama_client.fast_loopback
        self.assertEqual(f("http://localhost:11434"), "http://127.0.0.1:11434")
        self.assertEqual(f("localhost:11434"), "http://127.0.0.1:11434")
        self.assertEqual(f("http://127.0.0.1:11434"), "http://127.0.0.1:11434")
        self.assertEqual(f("http://[::1]:11434"), "http://[::1]:11434")

    def test_el_cliente_se_reutiliza_y_sigue_siendo_solo_local(self):
        cfg = {"ollama": {"host": "http://localhost:11434"}}
        self.assertIs(ollama_client.get_client(cfg), ollama_client.get_client(cfg))
        with self.assertRaises(RuntimeError):
            ollama_client.get_client({"ollama": {"host": "http://evil.example:11434"}})

    def test_un_error_permanente_no_se_reintenta(self):
        class Boom(Exception):
            status_code = 404
        client = mock.Mock()
        client.chat.side_effect = Boom("model not found")
        with mock.patch.object(ollama_client, "get_client", return_value=client), \
                mock.patch.object(ollama_client.time, "sleep") as sleep, self.assertRaises(RuntimeError):
            ollama_client.chat_stream({"ollama": {"retries": 2}}, "x", [], {})
        self.assertEqual(client.chat.call_count, 1)
        sleep.assert_not_called()

    def test_un_error_transitorio_se_reintenta_sin_dormir_tras_el_ultimo(self):
        client = mock.Mock()
        client.chat.side_effect = ConnectionError("caído")
        with mock.patch.object(ollama_client, "get_client", return_value=client), \
                mock.patch.object(ollama_client.time, "sleep") as sleep, self.assertRaises(RuntimeError):
            ollama_client.chat_stream({"ollama": {"retries": 2}}, "x", [], {})
        self.assertEqual(client.chat.call_count, 3)
        self.assertEqual(sleep.call_count, 2)           # entre intentos, no después del último


# ---------------------------------------------------------------------------------------------- configuración
class TestConfiguracion(unittest.TestCase):
    def test_lo_que_falta_se_toma_de_los_valores_por_defecto(self):
        merged = config._merge({"a": {"x": 1, "y": 2}, "b": 3}, {"a": {"y": 20}})
        self.assertEqual(merged, {"a": {"x": 1, "y": 20}, "b": 3})

    def test_config_parcial_o_vacia_no_rompe(self):
        with tempfile.TemporaryDirectory() as tmp:
            example = Path(tmp) / "example.yaml"
            example.write_text("ollama:\n  host: 'http://localhost:11434'\n  text_model: qwen2.5:7b\nlimits:\n  max_pages: 800\n",
                               encoding="utf-8")
            user = Path(tmp) / "config.yaml"
            for text, pages in (("", 800), ("limits:\n  max_pages: 5\n", 5)):
                user.write_text(text, encoding="utf-8")
                with mock.patch.object(config, "CONFIG_PATH", user), mock.patch.object(config, "CONFIG_EXAMPLE", example):
                    cfg = config.load_config(auto_hardware=False)
                self.assertEqual(cfg["limits"]["max_pages"], pages)
                self.assertEqual(cfg["ollama"]["text_model"], "qwen2.5:7b")

    def test_un_host_remoto_en_la_configuracion_se_rechaza(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "config.yaml"
            user.write_text("ollama:\n  host: 'http://10.0.0.5:11434'\n", encoding="utf-8")
            with mock.patch.object(config, "CONFIG_PATH", user), self.assertRaises(RuntimeError):
                config.load_config(auto_hardware=False)


# ---------------------------------------------------------------------------------------------- modelos y preferencias
class TestModelos(unittest.TestCase):
    def test_el_modelo_predeterminado_es_llama_y_admite_herramientas(self):
        self.assertEqual(DEFAULT_MODEL, "llama3.1:8b")
        self.assertIn(DEFAULT_MODEL, eng.CHAT_MODELS)
        self.assertIn(DEFAULT_MODEL, eng.TOOL_MODELS)
        self.assertEqual(DEFAULT_PREFS["model"], DEFAULT_MODEL)

    def test_cada_modelo_tiene_nombre_descripcion_y_detalle_unicos(self):
        labels = [eng.model_label(m) for m in eng.MODEL_INFO]
        self.assertEqual(len(labels), len(set(labels)))
        for model, (name, short, detail) in eng.MODEL_INFO.items():
            self.assertTrue(name and short and detail, model)
            self.assertIn(short, eng.model_label(model))
        self.assertEqual(set(eng.CHAT_MODELS), set(eng.MODEL_INFO))
        self.assertEqual(eng.model_label("desconocido:1b"), "desconocido:1b")
        self.assertEqual(eng.model_detail("desconocido:1b"), "")

    def test_las_preferencias_antiguas_pasan_a_llama_una_sola_vez(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "prefs.json"
            p.write_text('{"model": "qwen2.5:7b", "temperature": 0.3}', encoding="utf-8")       # sin versión: anteriores
            got = load_prefs(p)
            self.assertEqual(got["model"], DEFAULT_MODEL)
            self.assertEqual(got["temperature"], 0.3)                                            # el resto se respeta
            got["model"] = "qwen2.5:7b"                                                          # el usuario elige Qwen
            save_prefs(got, p)
            self.assertEqual(load_prefs(p)["model"], "qwen2.5:7b")                               # y se conserva
            self.assertEqual(load_prefs(p)["prefs_version"], PREFS_VERSION)

    def test_la_lista_de_modelos_instalados_se_recuerda_unos_segundos(self):
        client = mock.Mock()
        client.list.return_value = mock.Mock(models=[mock.Mock(model="llama3.1:8b")])
        engine = eng.ChatEngine({"ollama": {}})
        with mock.patch.object(eng, "get_client", return_value=client):
            self.assertEqual(engine.installed(), {"llama3.1:8b"})
            engine.installed()
            self.assertEqual(client.list.call_count, 1)
            engine.forget_installed()
            engine.installed()
            self.assertEqual(client.list.call_count, 2)

    def test_si_ollama_no_responde_no_se_recuerda_el_fallo(self):
        client = mock.Mock()
        client.list.side_effect = ConnectionError("apagado")
        engine = eng.ChatEngine({"ollama": {}})
        with mock.patch.object(eng, "get_client", return_value=client):
            self.assertEqual(engine.installed(), set())
            client.list.side_effect = None
            client.list.return_value = mock.Mock(models=[mock.Mock(model="x")])
            self.assertEqual(engine.installed(), {"x"})


# ---------------------------------------------------------------------------------------------- textutil
class TestTextUtil(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(safe_name('a<b>:"c"/d\\e|f?g*  h.pdf'), "a_b_c_d_e_f_g_h.pdf")
        self.assertEqual(safe_name("///"), "documento")
        self.assertLessEqual(len(safe_name("x" * 500)), 80)

    def test_deteccion_de_idioma(self):
        self.assertEqual(detect_language("El método de Simpson es una regla para integrar que se usa en los cálculos"), "es")
        self.assertEqual(detect_language("The method of Simpson is a rule that we use for the integrals of the functions"), "en")
        self.assertEqual(detect_language("1234 5678"), "es")

    def test_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.bin"
            p.write_bytes(b"abc")
            self.assertEqual(sha256_file(p), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
            self.assertEqual(sha256_file(p, chunk=1), sha256_file(p))


# ---------------------------------------------------------------------------------------------- línea de comandos
class TestLineaDeComandos(unittest.TestCase):
    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertEqual(out.getvalue().strip(), cli.__version__)

    def test_si_falta_una_dependencia_imprescindible_lo_dice_y_no_instala_nada(self):
        err = io.StringIO()
        with mock.patch.object(cli, "missing_dependencies", return_value=(["sympy"], [])), \
                contextlib.redirect_stderr(err), mock.patch("subprocess.check_call") as pip:
            self.assertEqual(cli.main([]), 1)
        pip.assert_not_called()
        self.assertIn("pip install -r requirements.txt", err.getvalue())
        self.assertIn("sympy", err.getvalue())

    def test_las_dependencias_declaradas_existen_en_requirements(self):
        text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(encoding="utf-8").lower()
        for package, _ in cli.DEPENDENCIES.values():
            self.assertIn(package.lower(), text, package)


# ---------------------------------------------------------------------------------------------- extractor
class TestExtractorLiberaRecursos(unittest.TestCase):
    """Al cancelar, el PDF debe cerrarse: en Windows un archivo abierto no se puede borrar ni mover."""

    def test_cancelar_deja_el_archivo_libre(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "a.pdf"
            doc = pymupdf.open()
            for n in range(3):
                doc.new_page().insert_textbox(pymupdf.Rect(50, 60, 540, 700), f"Página {n + 1}. " + "texto " * 60, fontsize=11)
            doc.save(str(pdf))
            doc.close()
            cancel = threading.Event()
            cfg = {"extraction": {"min_chars_per_page": 50}, "ollama": {"vision_model": "x"}}

            def progress(i, total, msg):
                if i == 1:
                    cancel.set()

            with self.assertRaises(Cancelled):
                extractor.extract_pages(pdf, "0" * 64, cfg, vision=mock.Mock(), progress=progress, cancel=cancel,
                                        math_vision=False)
            os.remove(pdf)                       # lanzaría PermissionError si el PDF siguiera abierto
            self.assertFalse(pdf.exists())

    def test_extraccion_normal(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "b.pdf"
            doc = pymupdf.open()
            doc.new_page().insert_textbox(pymupdf.Rect(50, 60, 540, 700), "La regla de Simpson aproxima integrales. " * 5, fontsize=11)
            doc.save(str(pdf))
            doc.close()
            cfg = {"extraction": {"min_chars_per_page": 50}, "ollama": {"vision_model": "x"}}
            pages, title, total = extractor.extract_pages(pdf, "1" * 64, cfg, vision=mock.Mock(), progress=lambda *a: None,
                                                          cancel=threading.Event(), math_vision=False)
            self.assertEqual((total, len(pages), pages[0].source), (1, 1, "texto"))
            self.assertIn("Simpson", pages[0].text)
            self.assertTrue(title)
            os.remove(pdf)


# ---------------------------------------------------------------------------------------------- índice de documentos
def _pdf(tmp: Path, name: str, text: str) -> Path:
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(50, 60, 540, 700), text, fontsize=11)
    path = tmp / name
    doc.save(str(path))
    doc.close()
    return path


def _embed(texts):
    """Embeddings de prueba: dos dimensiones, según aparezcan «alfa» o «beta»."""
    return [[float("alfa" in t.lower()), float("beta" in t.lower())] for t in texts]


class TestIndiceFiltradoYTransaccion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.a = _pdf(self.tmp, "a.pdf", "alfa alfa alfa. " + "El documento A habla de la cantidad alfa y su volumen. " * 3)
        self.b = _pdf(self.tmp, "b.pdf", "beta beta beta. " + "El documento B habla de la cantidad alfa de otra manera. " * 3)

    def tearDown(self):
        self._tmp.cleanup()

    def test_filtrar_por_documento_no_pierde_resultados_del_documento_elegido(self):
        for embed in (None, _embed):                # solo léxica y híbrida
            ix = DocIndex(None, ":memory:", embed_fn=embed)
            ra, rb = ix.add_pdf(self.a), ix.add_pdf(self.b)
            hits = ix.search("alfa", k=6, doc_ids=[rb.doc_id])
            self.assertTrue(hits)
            self.assertTrue(all(h.doc == "b.pdf" for h in hits))
            self.assertEqual({h.doc for h in ix.search("alfa", k=6)}, {"a.pdf", "b.pdf"})
            self.assertEqual(ix.search("alfa", doc_ids=[]), [])
            self.assertTrue(all(h.doc == "a.pdf" for h in ix.search("alfa", doc_ids=[ra.doc_id])))

    def test_un_fallo_a_mitad_no_deja_un_documento_a_medias(self):
        ix = DocIndex(None, ":memory:")
        real = ix.db
        three = pymupdf.open()                                  # tres páginas = al menos tres fragmentos
        for n in range(3):
            three.new_page().insert_textbox(pymupdf.Rect(50, 60, 540, 700), f"Página {n}. " + "contenido de relleno " * 12, fontsize=11)
        multi = self.tmp / "multi.pdf"
        three.save(str(multi))
        three.close()

        class Flaky:
            """Conexión que falla en la segunda inserción de fragmentos (como un disco lleno)."""
            def __init__(self):
                self.chunks = 0

            def execute(self, sql, *args):
                if sql.startswith("INSERT INTO chunks ("):
                    self.chunks += 1
                    if self.chunks == 2:
                        raise OSError("disco lleno")
                return real.execute(sql, *args)

            def __enter__(self):
                return real.__enter__()

            def __exit__(self, *exc):
                return real.__exit__(*exc)

        ix.db = Flaky()
        with self.assertRaises(OSError):
            ix.add_pdf(multi)
        ix.db = real
        self.assertEqual(len(ix.docs()), 0)
        self.assertEqual(real.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)
        self.assertEqual(real.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0], 0)
        self.assertFalse(ix.add_pdf(multi).already)         # y se puede volver a indexar sin quedar «ya indexado»

    def test_vectores_de_distinta_dimension_no_rompen_la_busqueda(self):
        ix = DocIndex(None, ":memory:", embed_fn=_embed)
        ix.add_pdf(self.a)
        ix._embed_fn = lambda texts: [[1.0, 0.0, 0.0] for _ in texts]       # se cambió de modelo de embeddings
        ix.add_pdf(self.b)
        ix._matrix = None
        self.assertTrue(ix.search("alfa"))                                  # no lanza ValueError


if __name__ == "__main__":
    unittest.main()
