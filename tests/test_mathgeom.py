"""Pruebas de la reconstrucción de fórmulas desde la geometría del PDF (local_ai_studio/mathgeom.py) y de su colocación en el texto.

No necesitan Ollama. Los glifos se construyen con las coordenadas reales de un PDF generado con LaTeX (fuentes
CMMI10, CMR10, CMR7, CMEX10…): las fuentes TeX no se pueden inventar con PyMuPDF, así que la geometría se fija a mano.
La prueba con un PDF real (opcional) solo corre si el archivo existe en tu equipo.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf

from local_ai_studio.pdf import mathgeom as mg
from local_ai_studio.pdf.cleaner import _is_fragment, page_text_for_clean


def gl(c, x0, x1, base, size=9.96, font="CMMI10"):
    """Glifo de texto corriente: caja de altura ~ tamaño, centrada en la línea base como en TeX."""
    return mg.Glyph(c, font, size, x0, base - 0.75 * size, x1, base + 0.25 * size, base)


def gb(c, x0, x1, y0, y1, base, size=9.96, font="CMEX10"):
    """Glifo con caja explícita (operadores grandes y límites, cuya caja no está centrada en la línea base)."""
    return mg.Glyph(c, font, size, x0, y0, x1, y1, base)


def rule(x0, x1, y):
    return mg.Bar(x0, x1, y)


class TestLayoutGeometry(unittest.TestCase):
    def test_fraccion_simple(self):
        # F=1/3: el eje de la fracción está en la línea base - 2,5 pt, numerador encima y denominador debajo
        gs = [gl("F", 120, 126, 153.0), gl("=", 130, 138, 153.0, font="CMR10"),
              gl("1", 140, 145, 146.3, font="CMR10"), gl("3", 140, 145, 159.8, font="CMR10")]
        latex, ok, why = mg.reconstruct(gs, [rule(140, 145, 150.5)])
        self.assertTrue(ok, why)
        self.assertEqual(latex, "F=\\frac{1}{3}")

    def test_fraccion_con_denominador_mas_ancho(self):
        # \frac{1}{10}: la regla cubre el denominador; el numerador queda centrado encima
        gs = [gl("1", 143, 148, 146.3, font="CMR10"), gl("1", 148, 153, 159.8, font="CMR10"),
              gl("0", 153, 158, 159.8, font="CMR10")]
        latex, ok, _ = mg.reconstruct(gs, [rule(143, 158, 150.5)])
        self.assertTrue(ok)
        self.assertEqual(latex, "\\frac{1}{10}")

    def test_superindice(self):
        gs = [gl("x", 50, 55, 100.0), gl("2", 56, 60, 96.0, size=6.97, font="CMR7")]
        self.assertEqual(mg.reconstruct(gs, [])[0], "x^2")

    def test_subindice_de_texto_romano(self):
        # T_{prom}: letras en CMR7 (romanas) bajadas 1,4 pt: se escriben como \mathrm
        gs = [gl("T", 50, 56, 100.0)] + [gl(c, 56 + 4 * k, 60 + 4 * k, 101.4, size=6.97, font="CMR7")
                                          for k, c in enumerate("prom")]
        self.assertEqual(mg.reconstruct(gs, [])[0], "T_{\\mathrm{prom}}")

    def test_integral_con_limites_a_la_derecha(self):
        # Q=\int_{1}^{7}x: límite superior 7 encima y límite inferior 1 debajo, a la derecha del signo
        gs = [gl("Q", 250, 258, 512.3), gl("=", 261, 269, 512.3, font="CMR10"),
              gb("Z", 271.6, 277.2, 498.1, 508.1, 498.8),
              gb("7", 281.6, 285.6, 495.8, 502.8, 501.2, size=6.97, font="CMR7"),
              gb("1", 277.2, 281.1, 516.0, 522.9, 521.4, size=6.97, font="CMR7"),
              gl("x", 300, 306, 512.3)]
        latex, ok, why = mg.reconstruct(gs, [])
        self.assertTrue(ok, why)
        self.assertEqual(latex, "Q=\\int_{1}^{7}x")

    def test_sumatorio_con_limites_centrados(self):
        # \sum_{i=0}^{6} x_i: límites centrados encima y debajo del ∑; el subíndice i de x va bajado
        gs = [gb("X", 282.8, 297.2, 479.0, 488.9, 479.6),
              gb("6", 288.1, 292.0, 471.2, 478.1, 476.6, size=6.97, font="CMR7"),
              gb("i", 283.6, 286.4, 495.4, 502.4, 500.8, size=6.97, font="CMMI7"),
              gb("=", 286.4, 292.5, 495.4, 502.4, 500.8, size=6.97, font="CMR7"),
              gb("0", 292.5, 296.5, 495.4, 502.4, 500.8, size=6.97, font="CMR7"),
              gl("x", 300, 306, 489.1),
              gl("i", 306, 309, 490.6, size=6.97, font="CMMI7")]
        latex, ok, why = mg.reconstruct(gs, [])
        self.assertTrue(ok, why)
        self.assertEqual(latex, "\\sum_{i=0}^{6}x_i")

    def test_dos_sumatorios_seguidos_cada_limite_a_su_sumatorio(self):
        # Dos ∑ a 20 pt: el límite j=0 es del segundo y no debe pegarse al primero
        gs = [gb("X", 282.8, 297.2, 479.0, 488.9, 479.6),
              gb("i", 283.6, 286.4, 495.4, 502.4, 500.8, size=6.97, font="CMMI7"),
              gb("=", 286.4, 292.5, 495.4, 502.4, 500.8, size=6.97, font="CMR7"),
              gb("0", 292.5, 296.5, 495.4, 502.4, 500.8, size=6.97, font="CMR7"),
              gb("X", 303.0, 317.4, 479.0, 488.9, 479.6),
              gb("j", 304.0, 307.0, 495.4, 502.4, 500.8, size=6.97, font="CMMI7"),
              gb("=", 307.0, 313.0, 495.4, 502.4, 500.8, size=6.97, font="CMR7"),
              gb("0", 313.0, 317.0, 495.4, 502.4, 500.8, size=6.97, font="CMR7"),
              gl("f", 322, 327, 489.1)]           # una fórmula real siempre tiene texto principal
        latex, ok, why = mg.reconstruct(gs, [])
        self.assertTrue(ok, why)
        self.assertEqual(latex, "\\sum_{i=0}\\sum_{j=0}f")

    def test_doble_integral_con_un_limite_es_iint(self):
        gs = [gb("Z", 271.6, 277.2, 498.1, 508.1, 498.8),
              gb("R", 277.0, 284.0, 516.0, 522.9, 521.4, size=6.97, font="CMR7"),
              gb("Z", 290.0, 295.6, 498.1, 508.1, 498.8),
              gl("f", 300, 305, 512.3)]
        latex, ok, why = mg.reconstruct(gs, [])
        self.assertTrue(ok, why)
        self.assertEqual(latex, "\\iint_{R} f")

    def test_espacio_solo_si_hay_hueco_real(self):
        # «dx dy» (hueco de 1,6 pt entre x y d) frente a «xy» pegado: TeX escribe el espacio fino explícito
        gs = [gl("d", 0, 5, 10), gl("x", 5, 10, 10), gl("d", 11.6, 16.6, 10), gl("y", 16.6, 21.6, 10)]
        self.assertEqual(mg.reconstruct(gs, [])[0], "dx dy")
        gs2 = [gl("x", 0, 5, 10), gl("y", 5, 10, 10)]
        self.assertEqual(mg.reconstruct(gs2, [])[0], "xy")

    def test_regla_fuera_de_la_banda_se_ignora(self):
        # Un borde de caja lejos de la fórmula no es una fracción: la fórmula sigue completa
        gs = [gl("F", 120, 126, 153.0), gl("=", 130, 138, 153.0, font="CMR10"),
              gl("1", 140, 145, 146.3, font="CMR10"), gl("3", 140, 145, 159.8, font="CMR10")]
        latex, ok, _ = mg.reconstruct(gs, [rule(140, 145, 150.5), rule(100, 200, 20.0)])
        self.assertTrue(ok)
        self.assertEqual(latex, "F=\\frac{1}{3}")

    def test_marco_de_caja_se_ignora(self):
        # Dos reglas del mismo ancho, una encima y otra debajo de la fórmula: es un recuadro, no contenido
        gs = [gl("T", 274.6, 280.5, 444.0), gl("=", 301.7, 309.5, 444.0, font="CMR10"),
              gl("3", 312.2, 317.2, 444.0, font="CMR10"), gl("0", 317.2, 322.2, 444.0, font="CMR10")]
        latex, ok, why = mg.reconstruct(gs, [rule(271.0, 338.0, 433.5), rule(271.0, 338.0, 450.1)])
        self.assertTrue(ok, why)
        self.assertEqual(latex, "T=30")

    def test_glifo_sin_mapeo_deja_el_bloque_incompleto(self):
        gs = [gl("F", 120, 126, 153.0), gl("€", 130, 138, 153.0, font="CMR10")]
        latex, ok, why = mg.reconstruct(gs, [])
        self.assertFalse(ok)
        self.assertEqual(latex, "")
        self.assertIn("sin mapeo", why)

    def test_subindice_sin_base_deja_el_bloque_incompleto(self):
        # Un subíndice que va antes de cualquier átomo no tiene a qué engancharse
        gs = [gl("2", 40, 44, 96.0, size=6.97, font="CMR7"), gl("x", 50, 55, 100.0)]
        self.assertFalse(mg.reconstruct(gs, [])[1])

    def test_fraccion_sin_denominador_es_incompleta(self):
        gs = [gl("1", 140, 145, 146.3, font="CMR10")]
        latex, ok, why = mg.reconstruct(gs, [rule(140, 145, 150.5)])
        self.assertFalse(ok)              # una regla sin contenido debajo no se puede interpretar: no se usa
        self.assertEqual(latex, "")


class TestCanonicoYComparacion(unittest.TestCase):
    def test_puntuacion_final_no_cuenta(self):
        self.assertTrue(mg.same_content("x=\\{1,2\\} ,", "x = \\{1, 2\\}"))
        self.assertFalse(mg.same_formula("x=\\{1,2\\} ,", "x = \\{1, 2\\}"))

    def test_los_digitos_si_cuentan(self):
        self.assertFalse(mg.same_content("12", "1,2"))
        self.assertFalse(mg.same_content("x=2", "x=3"))

    def test_estilos_equivalentes(self):
        self.assertTrue(mg.same_content("a \\times b", "a \\cdot b"))
        self.assertTrue(mg.same_content("T_{\\mathrm{prom}}", "T_{\\text{prom}}"))
        self.assertTrue(mg.same_content("30^{\\circ}C", "30^\\circ C"))

    def test_similitud_empareja_la_misma_formula(self):
        self.assertGreater(mg.similarity("F(1)=171", "F(1) = 171"), 0.9)
        self.assertLess(mg.similarity("F(1)=171", "Q=3240"), 0.5)


class TestDiferenciaParaElAviso(unittest.TestCase):
    def test_nombra_el_simbolo_que_difiere(self):
        self.assertEqual(mg.diff_hint(r"V=\int_{3}^{15}", r"V = \int_{2}^{15}"), "PDF «3» · visión «2»")

    def test_distingue_lo_que_solo_esta_en_el_pdf_o_en_la_vision(self):
        self.assertEqual(mg.diff_hint("y F(y)", "F(y)"), "solo en el PDF: «y»")
        self.assertEqual(mg.diff_hint("1,2", r"\{1,2\}"), r"solo en la visión: «\{»; solo en la visión: «\}»")

    def test_formulas_iguales_no_tienen_diferencia(self):
        self.assertEqual(mg.diff_hint("x=2,", "x = 2"), "forma distinta")


class TestColocacionEnTexto(unittest.TestCase):
    def _geo(self, latex, anchor, frags, complete=True):
        return {"latex": latex, "complete": complete, "why": "", "anchor": anchor, "frags": frags,
                "x0": 0, "y0": 0, "x1": 0, "y1": 0}

    def test_formula_tras_su_linea_de_prosa_y_fragmento_roto_fuera(self):
        raw = "Introducción\n\nEl área de la región es\n\nTprom = .\n\nSiguiente párrafo de prosa normal."
        anchor = mg._norm_text("El área de la región es")
        geo = [self._geo("T_{\\mathrm{prom}}=\\frac{1}{A}", anchor, ["Tprom=1A"])]
        out = page_text_for_clean(raw, "", [], [], geo)
        self.assertIn("El área de la región es\n\n$$ T_{\\mathrm{prom}}=\\frac{1}{A} $$", out)
        self.assertNotIn("Tprom = .", out)
        self.assertIn("Siguiente párrafo de prosa normal.", out)

    def test_sin_ancla_va_tras_la_formula_anterior(self):
        raw = "Primera línea de prosa.\n\nOtra línea de prosa que no aparece en la fórmula."
        anchor = mg._norm_text("Primera línea de prosa.")
        geo = [self._geo("x=1", anchor, []), self._geo("y=2", "anchorque no existe", [])]
        out = page_text_for_clean(raw, "", [], [], geo)
        self.assertLess(out.index("$$ x=1 $$"), out.index("$$ y=2 $$"))
        self.assertLess(out.index("$$ y=2 $$"), out.index("Otra línea"))

    def test_no_se_colocan_bloques_incompletos(self):
        raw = "Texto de prueba suficiente."
        out = page_text_for_clean(raw, "", [], [], [self._geo("", "", [], complete=False)])
        self.assertNotIn("$$", out)

    def test_aviso_si_la_vision_difiere_en_cifras(self):
        vis = "Texto de prueba suficiente.\n\n$$ x = 2 $$\n\nFin."
        eqs = [[0.5, "x = 2"]]
        geo = [self._geo("x=3", "", [])]
        out = page_text_for_clean("Texto de prueba suficiente.\n\nFin.", vis, eqs, [None], geo)
        self.assertIn("$$ x=3 $$ ⚠ la lectura visual difiere del PDF", out)
        self.assertIn("lectura alternativa: $ x = 2 $", out)

    def test_sin_aviso_si_solo_difiere_la_puntuacion(self):
        vis = "Texto de prueba suficiente.\n\n$$ x = 2, $$\n\nFin."
        eqs = [[0.5, "x = 2,"]]
        geo = [self._geo("x=2", "", [])]
        out = page_text_for_clean("Texto de prueba suficiente.\n\nFin.", vis, eqs, [None], geo)
        self.assertIn("$$ x=2 $$", out)
        self.assertNotIn("⚠", out)

    def test_fragmento_solo_si_es_formula_no_prosa(self):
        frags = {"tprom", "a", "r"}
        self.assertTrue(_is_fragment("Tprom = .", frags))
        self.assertTrue(_is_fragment("1 ZZ", frags))
        self.assertFalse(_is_fragment("Al observar los datos de la tabla", frags))
        self.assertFalse(_is_fragment("de la región", frags))


class TestPaginaRealDeTexto(unittest.TestCase):
    def test_pagina_de_prosa_y_codigo_no_produce_formulas(self):
        """Un PDF de texto normal (incluido código) no debe generar ninguna fórmula: el Taller no debe cambiar."""
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=300)
        page.insert_text((50, 60), "Esta es una frase de prosa normal con varias palabras.", fontsize=10)
        page.insert_text((50, 90), "function validar(clave) { return clave.length > 8; }", fontsize=9)
        page.insert_text((50, 120), "x = 1; y = 2;", fontsize=9)
        self.assertEqual(mg.page_formulas(page), [])


@unittest.skipUnless(os.path.exists(str(Path.home() / "Downloads" / "SIMPSOn (5) (2).pdf")),
                     "requiere el PDF de prueba SIMPSOn (5) (2).pdf en Descargas")
class TestPdfRealSimpson(unittest.TestCase):
    def test_formulas_clave_se_reconstruyen_completas(self):
        doc = pymupdf.open(str(Path.home() / "Downloads" / "SIMPSOn (5) (2).pdf"))
        latex = [e.latex for p in doc for e in mg.page_formulas(p) if e.complete]
        self.assertGreaterEqual(len(latex), 100)
        # Comparación canónica: ignora espacios y estilo, pero no un dígito, un signo ni un límite
        for esperada in ["Q=\\int_{1}^{7}\\int_{2}^{8}T(x,y)dx dy.", "F(1)=171.", "Q\\approx 1080.",
                         "T_{\\mathrm{prom}}=\\frac{1}{A}\\iint_{R} T(x,y)dA.",
                         "I\\approx \\frac{h_xh_y}{9}\\sum_{i=0}^{6}\\sum_{j=0}^{6}w_iw_jf(x_i,y_j)"]:
            self.assertTrue(any(mg.same_formula(l, esperada) for l in latex), esperada)


if __name__ == "__main__":
    unittest.main()
