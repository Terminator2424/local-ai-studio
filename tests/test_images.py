"""Pruebas de la edición de imágenes (Pillow). No necesitan GPU ni modelos."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from src.images import edit  # noqa: E402
from src.images.edit import EditParams  # noqa: E402


def sample(w=40, h=20):
    """Mitad izquierda roja, mitad derecha azul: permite saber si un giro o un volteo ha ocurrido."""
    im = Image.new("RGB", (w, h), (255, 0, 0))
    for x in range(w // 2, w):
        for y in range(h):
            im.putpixel((x, y), (0, 0, 255))
    return im


class TestEdicion(unittest.TestCase):
    def test_identidad_no_cambia_nada(self):
        im = sample()
        out = edit.apply(im, EditParams())
        self.assertEqual(out.tobytes(), im.tobytes())
        self.assertTrue(EditParams().is_identity())

    def test_no_modifica_la_original(self):
        im = sample()
        antes = im.tobytes()
        edit.apply(im, EditParams(grayscale=True, blur=2, rotate=90))
        self.assertEqual(im.tobytes(), antes)

    def test_giros_cambian_las_dimensiones(self):
        im = sample(40, 20)
        self.assertEqual(edit.apply(im, EditParams(rotate=90)).size, (20, 40))
        self.assertEqual(edit.apply(im, EditParams(rotate=180)).size, (40, 20))
        self.assertEqual(edit.apply(im, EditParams(rotate=270)).size, (20, 40))

    def test_giro_90_es_horario(self):
        # En horario, la mitad izquierda roja pasa a ser la mitad superior
        out = edit.apply(sample(40, 20), EditParams(rotate=90))
        self.assertEqual(out.getpixel((10, 2)), (255, 0, 0))
        self.assertEqual(out.getpixel((10, 37)), (0, 0, 255))

    def test_giro_no_multiplo_de_90_se_rechaza(self):
        with self.assertRaises(ValueError):
            edit.apply(sample(), EditParams(rotate=45))

    def test_volteo_horizontal_y_vertical(self):
        im = sample()
        h = edit.apply(im, EditParams(flip_h=True))
        self.assertEqual(h.getpixel((2, 5)), (0, 0, 255))
        v = edit.apply(im, EditParams(flip_v=True))
        self.assertEqual(v.getpixel((2, 5)), (255, 0, 0))     # el volteo vertical no cambia izquierda/derecha

    def test_recorte(self):
        out = edit.apply(sample(40, 20), EditParams(crop=(0.0, 0.0, 0.5, 1.0)))
        self.assertEqual(out.size, (20, 20))
        self.assertEqual(out.getpixel((10, 10)), (255, 0, 0))

    def test_recorte_invalido(self):
        for box in [(0.5, 0.0, 0.5, 1.0), (0.6, 0.0, 0.2, 1.0), (0.0, 0.0, 1.5, 1.0), (-0.1, 0.0, 1.0, 1.0)]:
            with self.assertRaises(ValueError, msg=str(box)):
                edit.apply(sample(), EditParams(crop=box))

    def test_tamano(self):
        self.assertEqual(edit.apply(sample(), EditParams(resize=(80, 10))).size, (80, 10))
        with self.assertRaises(ValueError):
            edit.apply(sample(), EditParams(resize=(0, 10)))
        with self.assertRaises(ValueError):
            edit.apply(sample(), EditParams(resize=(edit.MAX_SIDE + 1, 10)))

    def test_proporcion(self):
        self.assertEqual(edit.fit_size((400, 200), w=100), (100, 50))
        self.assertEqual(edit.fit_size((400, 200), h=50), (100, 50))
        self.assertEqual(edit.fit_size((400, 200)), (400, 200))

    def test_grises(self):
        out = edit.apply(sample(), EditParams(grayscale=True))
        r, g, b = out.getpixel((2, 2))
        self.assertTrue(r == g == b)

    def test_brillo_y_contraste(self):
        im = Image.new("RGB", (4, 4), (100, 100, 100))
        self.assertGreater(edit.apply(im, EditParams(brightness=1.5)).getpixel((0, 0))[0], 100)
        self.assertLess(edit.apply(im, EditParams(brightness=0.5)).getpixel((0, 0))[0], 100)

    def test_saturacion_cero_es_gris(self):
        r, g, b = edit.apply(sample(), EditParams(saturation=0.0)).getpixel((2, 2))
        self.assertTrue(abs(r - g) <= 1 and abs(g - b) <= 1)

    def test_sepia_tiñe_de_marron(self):
        im = Image.new("RGB", (4, 4), (128, 128, 128))
        r, g, b = edit.apply(im, EditParams(sepia=1.0)).getpixel((0, 0))
        self.assertTrue(r > g > b)

    def test_desenfoque_suaviza_el_borde(self):
        out = edit.apply(sample(), EditParams(blur=3))
        r, _, b = out.getpixel((20, 10))
        self.assertTrue(0 < r < 255 and 0 < b < 255)

    def test_conserva_el_canal_alfa(self):
        im = Image.new("RGBA", (6, 6), (10, 20, 30, 128))
        out = edit.apply(im, EditParams(grayscale=True))
        self.assertEqual(out.mode, "RGBA")
        self.assertEqual(out.getpixel((0, 0))[3], 128)

    def test_vista_previa_es_pequena_y_omite_el_resize(self):
        big = Image.new("RGB", (2000, 1000), (1, 2, 3))
        pv = edit.preview(big, EditParams(resize=(5000, 5000)), max_side=400)
        self.assertLessEqual(max(pv.size), 400)


class TestGuardado(unittest.TestCase):
    def test_guarda_en_los_tres_formatos(self):
        with tempfile.TemporaryDirectory() as d:
            for fmt, ext in edit.SAVE_FORMATS.items():
                p = edit.save(sample(), Path(d) / "x", fmt)
                self.assertEqual(p.suffix, ext)
                with Image.open(p) as saved:
                    self.assertEqual(saved.size, (40, 20))

    def test_jpeg_con_transparencia_se_aplana(self):
        with tempfile.TemporaryDirectory() as d:
            p = edit.save(Image.new("RGBA", (4, 4), (1, 2, 3, 50)), Path(d) / "t.jpg")
            with Image.open(p) as saved:
                self.assertEqual(saved.mode, "RGB")

    def test_formato_no_admitido(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                edit.save(sample(), Path(d) / "x", "BMP")

    def test_cargar_rechaza_lo_que_no_es_imagen(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "no.png"
            p.write_text("esto no es una imagen")
            with self.assertRaises(Exception):
                edit.load(p)


if __name__ == "__main__":
    unittest.main()
