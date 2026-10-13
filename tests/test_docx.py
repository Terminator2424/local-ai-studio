"""Pruebas de la exportación a Word (.docx)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docx import Document  # noqa: E402

from src.core.docx_export import md_to_docx  # noqa: E402

MD = r"""# Título del documento

Párrafo con **negrita**, *cursiva* y `código` en línea, y una fórmula en línea $x^2 + y^2 = r^2$ dentro.

## Sección

- Primer punto
- Segundo punto con $\frac{1}{3}$

1. Uno
2. Dos

$$ Q \approx \frac{1}{3}[171+4(174)+189] = 1080 $$

| Concepto | Resultado |
|---|---|
| Integral | 1080 |
| Promedio | 30 |

```python
def f(x):
    return x * 2
```

---

Fin.
"""


def build(md):
    d = tempfile.mkdtemp()
    p = md_to_docx(md, Path(d) / "salida.docx")
    return Document(str(p)), p


class TestDocx(unittest.TestCase):
    def test_estructura(self):
        doc, p = build(MD)
        self.assertGreater(p.stat().st_size, 5000)
        heads = [(par.style.name, par.text) for par in doc.paragraphs if par.style.name.startswith("Heading")]
        self.assertIn(("Heading 1", "Título del documento"), heads)
        self.assertIn(("Heading 2", "Sección"), heads)

    def test_formato_en_linea(self):
        doc, _ = build(MD)
        par = next(p for p in doc.paragraphs if p.text.startswith("Párrafo con"))
        bold = [r.text for r in par.runs if r.bold]
        ital = [r.text for r in par.runs if r.italic]
        mono = [r.text for r in par.runs if r.font.name == "Consolas"]
        self.assertEqual(bold, ["negrita"])
        self.assertEqual(ital, ["cursiva"])
        self.assertEqual(mono, ["código"])

    def test_formulas_son_imagenes(self):
        doc, _ = build(MD)
        self.assertGreaterEqual(len(doc.inline_shapes), 3)            # en línea, en la lista y en bloque

    def test_listas_tabla_y_codigo(self):
        doc, _ = build(MD)
        styles = [p.style.name for p in doc.paragraphs]
        self.assertEqual(styles.count("List Bullet"), 2)
        self.assertEqual(styles.count("List Number"), 2)
        self.assertEqual(len(doc.tables), 1)
        t = doc.tables[0]
        self.assertEqual([c.text for c in t.rows[0].cells], ["Concepto", "Resultado"])
        self.assertEqual([c.text for c in t.rows[2].cells], ["Promedio", "30"])
        self.assertTrue(any("def f(x):" in p.text for p in doc.paragraphs))

    def test_formula_en_bloque_va_centrada(self):
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        doc, _ = build(MD)
        centered = [p for p in doc.paragraphs if p.alignment == WD_ALIGN_PARAGRAPH.CENTER]
        self.assertTrue(centered)

    def test_texto_hostil_no_rompe_ni_se_interpreta(self):
        evil = ("# <script>alert(1)</script>\n\n<img src='http://evil.example/x.png'> [clic](http://evil.example)\n\n"
                "Control \x00\x01\x02 y \x0b fin. =cmd|' /C calc'!A0\n\n$$ \\badcommand{ $$\n\n| a |\n|---|\n")
        doc, _ = build(evil)
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("<script>", text)                       # literal, no se ejecuta ni se oculta
        self.assertNotIn("\x00", text)
        # sin enlaces ni imágenes externas en el paquete
        rels = [r.target_ref for r in doc.part.rels.values()]
        self.assertFalse(any("evil.example" in r for r in rels), rels)

    def test_vacio_y_enorme(self):
        build("")
        doc, _ = build("palabra " * 50_000)
        self.assertGreater(len(doc.paragraphs), 0)

    def test_formula_no_compatible_cae_a_texto(self):
        doc, _ = build(r"$$ \weirdunsupported{x} $$")
        self.assertTrue(doc.paragraphs)


if __name__ == "__main__":
    unittest.main()
