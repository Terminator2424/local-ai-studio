"""Pruebas del uso de disco y la limpieza segura (solo se borra lo regenerable, lo más antiguo primero)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_ai_studio.core import storage


def write(path: Path, size: int, age_days: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = path.stat().st_mtime - age_days * 86400
    os.utime(path, (t, t))
    return path


class TestAlmacenamiento(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.cache = self.base / "resumenes" / ".cache"
        self.pages = self.base / "imagenes"
        patcher = mock.patch.object(storage, "BASE_DIR", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_tamano_de_carpeta(self):
        write(self.cache / "a.json", 100)
        write(self.cache / "sub" / "b.json", 250)
        self.assertEqual(storage.folder_size(self.cache), 350)
        self.assertEqual(storage.folder_size(self.base / "no_existe"), 0)

    def test_la_poda_borra_primero_lo_mas_antiguo(self):
        old = write(self.cache / "old.json", 400, age_days=30)
        mid = write(self.cache / "mid.json", 400, age_days=10)
        new = write(self.cache / "new.json", 400, age_days=1)
        freed = storage.prune(self.cache, 800)
        self.assertEqual(freed, 400)
        self.assertFalse(old.exists())
        self.assertTrue(mid.exists() and new.exists())
        self.assertEqual(storage.prune(self.cache, 800), 0)         # ya está por debajo del tope: no hace nada

    def test_vaciar_conserva_el_estado_de_la_interfaz(self):
        write(self.cache / "abc_final_123.json", 50)
        write(self.cache / "sub" / "def_map_456.json", 50)
        keep1, keep2 = write(self.cache / "queue.json", 10), write(self.cache / "ui.json", 10)
        with mock.patch.object(storage, "_BY_KEY", {"cache": storage.Area("cache", "c", self.cache, "", True)}):
            self.assertEqual(storage.clear("cache"), 100)
        self.assertTrue(keep1.exists() and keep2.exists())
        self.assertFalse((self.cache / "sub").exists())             # las carpetas vacías también se quitan

    def test_solo_se_pueden_limpiar_las_areas_regenerables(self):
        for key in ("models", "outputs", "chats", "summaries", "logs", "inexistente"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                storage.clear(key)

    def test_se_niega_a_podar_fuera_de_la_aplicacion(self):
        with tempfile.TemporaryDirectory() as other:
            victim = write(Path(other) / "importante.docx", 10)
            with self.assertRaises(ValueError):
                storage.prune(Path(other), 0)
            self.assertTrue(victim.exists())

    def test_el_uso_cuenta_cada_byte_una_sola_vez(self):
        write(self.base / "resumenes" / "resumen.md", 1000)
        write(self.cache / "x.json", 300)
        folder = {"summaries": "resumenes", "cache": "resumenes/.cache", "pages": "imagenes"}
        relocated = tuple(storage.Area(a.key, a.title, self.base / folder.get(a.key, a.key), a.note, a.clearable)
                          for a in storage.AREAS)
        with mock.patch.object(storage, "AREAS", relocated):
            sizes = {a.key: n for a, n in storage.usage()}
        self.assertEqual(sizes["cache"], 300)
        self.assertEqual(sizes["summaries"], 1000)                  # sin contar la caché que vive dentro

    def test_formato_legible(self):
        self.assertEqual(storage.human(512), "512 B")
        self.assertEqual(storage.human(2048), "2.0 KB")
        self.assertEqual(storage.human(5 * 1024 ** 2), "5.0 MB")
        self.assertEqual(storage.human(3 * 1024 ** 3), "3.0 GB")


if __name__ == "__main__":
    unittest.main()
