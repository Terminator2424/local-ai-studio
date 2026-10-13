"""Pruebas del modo estudiar: verificación de pasos, bloques de código y tarjetas."""
import csv
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_ai_studio.study import check, tasks


class TestAnotacion(unittest.TestCase):
    def test_marca_verificado_incorrecto_y_sin_verificar(self):
        md, st = check.annotate("Bien $2 + 2 = 4$, mal $2 + 2 = 5$ y definición $A = 36$.")
        self.assertIn("$2 + 2 = 4$ ✓", md)
        self.assertRegex(md, r"\$2 \+ 2 = 5\$ ✗ no cuadra")
        self.assertIn("$A = 36$ ⚠ sin verificar", md)
        self.assertEqual((st.ok, st.error, st.unknown), (1, 1, 1))

    def test_algebra_simbolica(self):
        md, st = check.annotate("$$ (x+1)^2 = x^2 + 2x + 1 $$\n\n$$ (x+1)^2 = x^2 + 1 $$")
        self.assertIn("✓", md.splitlines()[0])
        self.assertIn("✗", md.splitlines()[2])
        self.assertEqual((st.ok, st.error), (1, 1))

    def test_sin_igualdad_no_se_toca(self):
        md, st = check.annotate("La variable $x$ y el término $\\frac{1}{3}$ no son igualdades.")
        self.assertNotIn("✓", md)
        self.assertNotIn("⚠", md)
        self.assertEqual((st.ok, st.error, st.unknown), (0, 0, 0))
        self.assertEqual(st.summary(), "Sin igualdades que comprobar.")

    def test_cadena_numerica_larga_de_simpson(self):
        md, st = check.annotate(r"$$ F(1) = \frac{1}{3}[27+110+56+114+58+118+30] = \frac{513}{3} = 171 $$")
        self.assertEqual(st.error, 0)
        md2, st2 = check.annotate(r"$$ \frac{1}{3}[27+110+56+114+58+118+30] = \frac{514}{3} $$")
        self.assertEqual(st2.error, 1)

    def test_resumen(self):
        _, st = check.annotate("$1+1=2$ $2+2=5$ $B = 3$")
        self.assertEqual(st.summary(), "Igualdades: 1 verificadas ✓ · 1 incorrectas ✗ · 1 sin verificar ⚠ (de 3)")

    def test_bloques_de_codigo(self):
        md = "Texto\n```python\nprint(1)\n```\nmás\n```\nsolo\n```\n"
        self.assertEqual(check.code_blocks(md), [("python", "print(1)"), ("", "solo")])


class TestTarjetas(unittest.TestCase):
    def test_json_correcto_con_texto_alrededor(self):
        t = 'Aquí tienes:\n[{"q": "¿Qué es una integral?", "a": "El área bajo la curva."}, {"q": "Simpson", "a": "Usa parábolas"}]\nSuerte'
        cards = tasks.parse_flashcards(t)
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["a"], "El área bajo la curva.")

    def test_json_roto_o_con_tipos_erroneos(self):
        self.assertEqual(tasks.parse_flashcards("no hay nada"), [])
        self.assertEqual(tasks.parse_flashcards('[{"q": "a", "a": '), [])
        self.assertEqual(tasks.parse_flashcards('[{"q": 5, "a": "x"}, {"q": "ok", "a": "bien"}, {"x": 1}, "texto", {"q": " ", "a": "y"}]'), [
            {"q": "ok", "a": "bien"}])

    def test_tope_de_tarjetas_y_longitud(self):
        import json
        big = json.dumps([{"q": f"p{i}", "a": "r" * 5000} for i in range(100)])
        cards = tasks.parse_flashcards(big)
        self.assertEqual(len(cards), 40)
        self.assertLessEqual(len(cards[0]["a"]), 1200)

    def test_el_json_no_se_evalua(self):
        cards = tasks.parse_flashcards('[{"q": "__import__(\'os\').system(\'x\')", "a": "inofensivo"}]')
        self.assertEqual(cards[0]["q"], "__import__('os').system('x')")

    def test_csv_neutraliza_formulas_de_hoja_de_calculo(self):
        cards = [{"q": "=cmd|' /C calc'!A0", "a": "+1+1"}, {"q": "normal, con coma", "a": '"comillas"'}]
        rows = list(csv.reader(io.StringIO(tasks.cards_csv(cards))))
        self.assertTrue(rows[0][0].startswith("'="))
        self.assertTrue(rows[0][1].startswith("'+"))
        self.assertEqual(rows[1], ["normal, con coma", '"comillas"'])

    def test_markdown(self):
        self.assertEqual(tasks.cards_markdown([{"q": "P", "a": "R"}]), "**1. P**\n\n> R")

    def test_mensajes_tratan_el_documento_como_datos(self):
        m = tasks.messages("solve", "Texto del doc", "Calcula 2+2")
        self.assertIn("DATOS", m[0]["content"])
        self.assertIn("DOCUMENTO (datos)", m[1]["content"])
        self.assertIn("EJERCICIO", m[1]["content"])
        self.assertEqual(set(tasks.TASKS.values()), {"flashcards", "practice", "solve"})


if __name__ == "__main__":
    unittest.main()
