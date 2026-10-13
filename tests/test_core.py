"""Pruebas automáticas (no necesitan Ollama): python -m unittest discover -s tests -v

Generan PDFs sintéticos con PyMuPDF y comprueban que el modo «Texto limpio» conserva código, tablas y orden de lectura,
y que las verificaciones exactas (cadenas numéricas, código, host de Ollama, hardware) funcionan.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf

from local_ai_studio.pdf.cleaner import clean_text
from local_ai_studio.core.hardware_detector import HardwareInfo, decide
from local_ai_studio.pdf.layout import layout_text
from local_ai_studio.pdf.scrub import fix_plain_tables, scrub_lines
from local_ai_studio.core.ollama_client import enforce_local_host
from local_ai_studio.pdf.verify import check_code_block, compare_readings, mark_numeric_errors, numeric_chain_error

CODE = ["function validar(clave) {",
        "  if (clave.length < 8) return false;",
        "  if (!clave.match(/[A-Z]/)) return false;",
        "  return true;",
        "}"]


def _page_with(doc, items):
    page = doc.new_page()
    for kind, x, y, text in items:
        if kind == "mono":
            page.insert_text((x, y), text, fontname="cour", fontsize=9)
        else:
            page.insert_text((x, y), text, fontname="helv", fontsize=10)
    return page


class TestExtraccion(unittest.TestCase):
    def test_codigo_tabla_y_titulos(self):
        doc = pymupdf.open()
        items = [("txt", 60, 60, "1. Objetivos del taller"),
                 ("txt", 60, 90, "Explique el concepto de complejidad ciclomatica y calcule la metrica de calidad del software.")]
        for k, ln in enumerate(CODE):
            items.append(("mono", 60, 130 + 12 * k, ln))
        items.append(("txt", 60, 230, "Escala de interpretacion"))
        rows = [("V(G)", "Riesgo", "Interpretacion"), ("1 - 10", "Bajo", "Codigo simple"),
                ("11 - 20", "Moderado", "Vigilar"), ("21 - 50", "Alto", "Refactorizar")]
        for k, (a, b, c) in enumerate(rows):
            for x, t in ((60, a), (180, b), (300, c)):
                items.append(("txt", x, 260 + 16 * k, t))
        _page_with(doc, items)
        md = clean_text(layout_text(doc[0]))
        self.assertIn("## 1. Objetivos del taller", md)
        self.assertIn("```javascript", md)
        for ln in CODE:                                  # el código se conserva EXACTO, con su sangría
            self.assertIn(ln, md)
        self.assertIn("| V(G) | Riesgo | Interpretacion |", md)
        self.assertIn("| 21 - 50 | Alto | Refactorizar |", md)

    def test_dos_columnas_orden_de_lectura(self):
        doc = pymupdf.open()
        items = []
        for k in range(30):
            items.append(("txt", 50, 60 + 14 * k, f"izquierda linea numero {k:02d} con texto suficiente para llenar"))
            items.append(("txt", 320, 60 + 14 * k, f"derecha linea numero {k:02d} con texto suficiente para llenar"))
        _page_with(doc, items)
        text = layout_text(doc[0])
        self.assertLess(text.index("izquierda linea numero 29"), text.index("derecha linea numero 00"))


class TestVerificacion(unittest.TestCase):
    def test_cadena_numerica(self):
        self.assertIsNone(numeric_chain_error(r"F(1) = \frac{1}{3} [ 27 + 110 + 56 + 114 + 58 + 118 + 30 ] = \frac{513}{3} = 171."))
        self.assertIsNotNone(numeric_chain_error(r"F(2) = \frac{1}{3} [ 27.5 + 112 + 57 + 116 + 59 + 120 + 30.5 ] = \frac{514}{3} = 171.33"))
        self.assertIsNone(numeric_chain_error(r"x = \{1, 2, 3\}"))
        self.assertIsNone(numeric_chain_error(r"A = (8 - 2)(7 - 1) = 6(6) = 36 \, m^2."))

    def test_marca_en_markdown(self):
        md, errs = mark_numeric_errors("ok $2 + 2 = 4$ y mal $2 + 2 = 5$")
        self.assertEqual(len(errs), 1)
        self.assertIn("$2 + 2 = 5$ ⚠", md)

    def test_doble_lectura(self):
        chk = compare_readings([[0, r"F = \frac{999}{3}"]], [[0, r"F = \frac{513}{3}"]], "F 513 3")[0]
        self.assertFalse(chk.ok)
        self.assertTrue(chk.alt)
        self.assertTrue(compare_readings([[0, r"F = \frac{513}{3}"]], [[0, r"F = \frac{513}{3}"]], "F 513 3")[0].ok)

    def test_codigo(self):
        raw = "\n".join(CODE)
        self.assertEqual(check_code_block(raw, "javascript", raw), [])
        self.assertTrue(check_code_block(raw.replace("}", "", 1), "javascript", raw))      # llave sin cerrar
        self.assertTrue(check_code_block("def f(:\n  pass", "python", "def f(:\n  pass"))   # sintaxis Python

    def test_saneado(self):
        out, _ = scrub_lines("- dato 4242\n- ok", "ok 17")
        self.assertNotIn("4242", out)                      # cifra que no está en la fuente → se elimina
        md, n = fix_plain_tables("1 2 3 4 5\n6 7 8 9 10\n11 12 13 14 15")
        self.assertEqual(n, 1)
        self.assertIn("|---|", md)


class TestSeguridadYHardware(unittest.TestCase):
    def test_ollama_solo_localhost(self):
        for ok in ("http://localhost:11434", "127.0.0.1:11434", "http://[::1]:11434"):
            enforce_local_host(ok)
        for bad in ("http://0.0.0.0:11434", "http://192.168.1.5:11434", "http://ejemplo.com"):
            with self.assertRaises(RuntimeError):
                enforce_local_host(bad)

    def test_hardware(self):
        cfg = {"hardware": {}, "ollama": {"text_model": "qwen2.5:7b", "vision_model": "qwen2.5vl:7b",
                                          "text_options": {"num_ctx": 16384}, "vision_options": {"num_ctx": 8192}}}
        inst = {"qwen2.5:14b", "qwen2.5:7b", "qwen2.5:3b"}
        big = decide(HardwareInfo("RTX 3090", "nvidia", 24, 22, 64), inst, cfg, 22)
        self.assertEqual(big.text_model, "qwen2.5:14b")
        small = decide(HardwareInfo("GTX 1650", "nvidia", 4, 3.4, 16), inst, cfg, 3.4)
        self.assertEqual(small.text_model, "qwen2.5:7b")
        self.assertLessEqual(small.text_num_ctx, 4096)
        cpu = decide(HardwareInfo("Sin GPU", "cpu", 0, 0, 8), inst, cfg)
        self.assertEqual(cpu.text_num_gpu, 0)



class TestRegresiones(unittest.TestCase):
    def test_regex_en_codigo_no_es_falso_positivo(self):
        code = ('function validarClave(clave) {\n  if (clave.length < 8) return false;\n  if (!clave.match(/[A-Z]/)) return false;\n'
                '  if (!clave.match(/[!@#$%]/)) return false;\n  return true;\n}')
        self.assertEqual(check_code_block(code, "javascript", code), [])

    def test_lineas_numericas_sueltas_no_se_borran(self):
        from local_ai_studio.pdf.extractor import _strip_headers_footers
        pages = ["F(1) = [27 + 110]\n\n513\n\n3\n\ntexto final de la pagina uno\n\n1", "otra pagina\n\n2"]
        out = _strip_headers_footers(pages)
        self.assertIn("513", out[0])           # numerador de una fracción: contenido, no número de página
        self.assertNotIn("\n1", "\n" + out[0].splitlines()[-1] if out[0].splitlines()[-1] == "1" else "")


class TestMathOCR(unittest.TestCase):
    def test_simbolos_ignoran_la_estructura(self):
        from local_ai_studio.pdf.mathocr import symbol_tokens
        a = symbol_tokens(r"F(y) \approx \frac{h_x}{3} [ T_0 + 4T_1 ]")
        b = symbol_tokens(r"F(y)\approx\underbrace{h_{x}}_{3}\left[T_{0}+4T_{1}\right]")
        self.assertEqual(a, b)                      # fracción vs underbrace: mismos símbolos
        c = symbol_tokens(r"F(y) \approx \frac{h_x}{3} [ T_0 - 4T_1 ]")
        self.assertNotEqual(a, c)                   # un signo cambiado sí se detecta


if __name__ == "__main__":
    unittest.main()
