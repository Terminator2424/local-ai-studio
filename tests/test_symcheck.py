"""Pruebas de la verificación simbólica (sympy): detecta errores de álgebra y NO marca definiciones ni lo ambiguo."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pdf import symcheck as sc  # noqa: E402
from src.pdf.verify import chain_error, mark_numeric_errors  # noqa: E402


class TestIdentidades(unittest.TestCase):
    def test_identidades_correctas(self):
        for f in [r"(x+1)^2 = x^2 + 2x + 1", r"\frac{x^2-1}{x-1} = x+1", r"2x + 3x = 5x", r"a_1 + a_2 = a_2 + a_1",
                  r"(a+b)(a-b) = a^2 - b^2", r"\sqrt{16} = 4", r"\frac{h_x}{3} \cdot 3 = h_x", r"x^{-1} = \frac{1}{x}"]:
            self.assertIsNone(sc.symbolic_chain_error(f), f)

    def test_errores_de_algebra_se_detectan(self):
        for f in [r"(x+1)^2 = x^2 + 1", r"2x + 3x = 6x", r"(a+b)^2 = a^2 + b^2", r"\frac{x^2-1}{x-1} = x-1",
                  r"y = 2x + 1 = 2(x+1)"]:
            self.assertIsNotNone(sc.symbolic_chain_error(f), f)

    def test_errores_numericos_con_raices_y_pi(self):
        self.assertIsNone(sc.symbolic_chain_error(r"\sqrt{2}\sqrt{2} = 2"))
        self.assertIsNotNone(sc.symbolic_chain_error(r"\sqrt{16} = 5"))
        self.assertIsNone(sc.symbolic_chain_error(r"2\pi \approx 6.28"))
        self.assertIsNotNone(sc.symbolic_chain_error(r"2\pi \approx 7"))


class TestNoFalsosPositivos(unittest.TestCase):
    def test_definiciones_no_son_identidades(self):
        for f in [r"A = 6(6) = 36", r"h_x = \frac{8-2}{6} = 1", r"x = 5", r"T_{prom} = 30", r"n_x = n_y = 6"]:
            self.assertIsNone(sc.symbolic_chain_error(f), f)

    def test_lo_ambiguo_se_abstiene(self):
        for f in [r"F(1) = 171", r"T(x,y) = 3", r"f(x) = x^2", r"x = \{1, 2, 3\}", r"30^\circ C = 30", r"36 \, m^2 = 36",
                  r"\int_0^1 x\,dx = \frac{1}{2}", r"\sum_{i=0}^{6} i = 21", r"1 \leq x \leq 7", r"x(y+1) = xy + x"]:
            self.assertIsNone(sc.symbolic_chain_error(f), f)

    def test_redondeo_declarado_es_valido(self):
        self.assertIsNone(sc.symbolic_chain_error(r"\frac{1}{3} = 0.33"))
        self.assertIsNone(sc.symbolic_chain_error(r"\frac{2}{3} \approx 0.67"))
        self.assertIsNotNone(sc.symbolic_chain_error(r"\frac{1}{3} = 0.4"))

    def test_basura_y_entradas_hostiles_no_rompen(self):
        for f in ["", "=", "= =", r"\frac{1}", r"x^", "((((((((", "a" * 5000, r"\frac{1}{0} = 1", r"x^{99999999} = 1",
                  "__import__('os').system('x') = 1", r"10^{12} \cdot 10^{12} = 10^{24}"]:
            sc.symbolic_chain_error(f)          # no debe lanzar ninguna excepción

    def test_equivalent(self):
        self.assertTrue(sc.equivalent("(a+b)^2", "a^2+2ab+b^2"))
        self.assertFalse(sc.equivalent("a+b", "a-b"))
        self.assertIsNone(sc.equivalent("F(1)", "2"))


class TestIntegracionConVerify(unittest.TestCase):
    def test_chain_error_une_aritmetica_y_simbolica(self):
        self.assertIsNone(chain_error(r"F(1) = \frac{1}{3} [ 27 + 110 + 56 + 114 + 58 + 118 + 30 ] = \frac{513}{3} = 171."))
        self.assertIsNone(chain_error(r"\frac{514}{3} = 171.33"))                    # 514/3 = 171,33: redondeo válido
        self.assertIsNotNone(chain_error(                                            # la suma real es 522, no 514
            r"F(2) = \frac{1}{3} [ 27.5 + 112 + 57 + 116 + 59 + 120 + 30.5 ] = \frac{514}{3}"))
        self.assertIsNotNone(chain_error(r"(x+1)^2 = x^2 + 1"))                      # simbólica (nueva)

    def test_se_marca_en_el_markdown(self):
        md, errs = mark_numeric_errors("bien $(x+1)^2 = x^2 + 2x + 1$ y mal $(x+1)^2 = x^2 + 1$")
        self.assertEqual(len(errs), 1)
        self.assertIn("x^2 + 1$ ⚠", md)


@unittest.skipUnless(os.path.exists(str(Path.home() / "Downloads" / "SIMPSOn (5) (2).pdf")),
                     "requiere el PDF de prueba SIMPSOn (5) (2).pdf en Descargas")
class TestDocumentoReal(unittest.TestCase):
    def test_ninguna_formula_correcta_del_pdf_se_marca(self):
        """El documento de Simpson es correcto: la verificación NO puede marcar ninguna de sus fórmulas."""
        import pymupdf
        from src.pdf import mathgeom
        doc = pymupdf.open(str(Path.home() / "Downloads" / "SIMPSOn (5) (2).pdf"))
        total, marcadas = 0, []
        for page in doc:
            for g in mathgeom.page_formulas(page):
                if g.complete:
                    total += 1
                    err = chain_error(g.latex)
                    if err:
                        marcadas.append((g.latex, err))
        self.assertGreater(total, 100)
        self.assertEqual(marcadas, [])


if __name__ == "__main__":
    unittest.main()
