"""Pruebas del catálogo de modelos, la verificación de hashes y los parámetros de generación.

La prueba con GPU real solo corre si PyTorch ve CUDA y SD 1.5 está descargado; si no, se salta.
"""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from local_ai_studio.images import generate as gen
from local_ai_studio.images import models as mdl

SD15 = mdl.CATALOG["sd15"]


class TestCatalogo(unittest.TestCase):
    def test_revision_y_hashes_fijados(self):
        for m in mdl.CATALOG.values():
            self.assertRegex(m.revision, r"^[0-9a-f]{40}$", m.key)
            weights = [r for r in m.files if r.endswith(".safetensors")]
            self.assertTrue(weights, m.key)
            for rel in weights:
                self.assertRegex(m.files[rel], r"^[0-9a-f]{64}$", f"{m.key}/{rel} sin SHA-256 fijado")

    def test_solo_safetensors_y_texto_nunca_pickle(self):
        for m in mdl.CATALOG.values():
            for rel in m.files:
                self.assertRegex(rel, r"\.(json|txt|safetensors)$", rel)
                self.assertNotRegex(rel, r"\.(bin|ckpt|pt|pth|pkl)$")

    def test_resoluciones_nativas(self):
        self.assertEqual(mdl.CATALOG["sd15"].native, 512)
        self.assertEqual(mdl.CATALOG["sdxl"].native, 1024)
        self.assertTrue(mdl.CATALOG["sdxl"].xl and not mdl.CATALOG["sd15"].xl)


class TestHashes(unittest.TestCase):
    def _model(self, tmp: Path, content=b"pesos de prueba"):
        sha = hashlib.sha256(content).hexdigest()
        m = mdl.ImageModel(key="t", title="t", repo="x/y", revision="0" * 40, native=512, license="x",
                           files={"unet/w.safetensors": sha, "model_index.json": ""})
        patcher = mock.patch.object(mdl, "MODELS_DIR", tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        return m, content

    def test_verify_acepta_el_archivo_correcto(self):
        with tempfile.TemporaryDirectory() as d:
            m, c = self._model(Path(d))
            (m.dir / "unet").mkdir(parents=True)
            (m.dir / "unet/w.safetensors").write_bytes(c)
            (m.dir / "model_index.json").write_text("{}")
            self.assertEqual(mdl.verify(m), [])
            self.assertTrue(mdl.is_installed(m))

    def test_verify_detecta_un_archivo_manipulado(self):
        with tempfile.TemporaryDirectory() as d:
            m, c = self._model(Path(d))
            (m.dir / "unet").mkdir(parents=True)
            (m.dir / "unet/w.safetensors").write_bytes(c + b"x")
            (m.dir / "model_index.json").write_text("{}")
            self.assertEqual(mdl.verify(m), ["unet/w.safetensors"])

    def test_descarga_con_hash_incorrecto_borra_el_archivo_y_avisa(self):
        with tempfile.TemporaryDirectory() as d:
            m, c = self._model(Path(d))

            def fake_download(repo, rel, revision, local_dir):
                p = Path(local_dir) / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"contenido malicioso")
                return str(p)
            with mock.patch("huggingface_hub.hf_hub_download", side_effect=fake_download):
                with self.assertRaises(mdl.HashMismatch):
                    mdl.download(m)
            self.assertFalse((m.dir / "unet/w.safetensors").exists())

    def test_descarga_correcta_pide_la_revision_fijada(self):
        with tempfile.TemporaryDirectory() as d:
            m, c = self._model(Path(d))
            calls = []

            def fake_download(repo, rel, revision, local_dir):
                calls.append((repo, revision))
                p = Path(local_dir) / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(c if rel.endswith(".safetensors") else b"{}")
                return str(p)
            with mock.patch("huggingface_hub.hf_hub_download", side_effect=fake_download):
                mdl.download(m)
            self.assertTrue(all(r == ("x/y", "0" * 40) for r in calls))
            self.assertEqual(mdl.verify(m), [])

    def test_no_vuelve_a_descargar_lo_que_ya_esta_bien(self):
        with tempfile.TemporaryDirectory() as d:
            m, c = self._model(Path(d))
            (m.dir / "unet").mkdir(parents=True)
            (m.dir / "unet/w.safetensors").write_bytes(c)
            (m.dir / "model_index.json").write_text("{}")
            with mock.patch("huggingface_hub.hf_hub_download") as dl:
                mdl.download(m)
            dl.assert_not_called()


class TestParametros(unittest.TestCase):
    def ok(self, **kw):
        return gen.GenParams(prompt="un gato", **kw).validate(SD15)

    def test_valores_por_defecto_son_validos(self):
        self.ok()

    def test_prompt_vacio(self):
        with self.assertRaises(gen.GenerationError):
            gen.GenParams(prompt="   ").validate(SD15)

    def test_rangos(self):
        for kw in [dict(steps=0), dict(steps=101), dict(cfg=0.5), dict(cfg=25), dict(width=100), dict(width=517),
                   dict(height=2048), dict(batch=0), dict(batch=5), dict(seed=-1), dict(seed=2 ** 32)]:
            with self.assertRaises(gen.GenerationError, msg=str(kw)):
                self.ok(**kw)

    def test_resolucion_muy_por_encima_de_la_nativa_se_rechaza(self):
        with self.assertRaises(gen.GenerationError):
            self.ok(width=1024, height=1024)            # SD 1.5 a 1024: imágenes duplicadas
        gen.GenParams(prompt="x", width=1024, height=1024).validate(mdl.CATALOG["sdxl"])

    def test_semillas_reproducibles_y_consecutivas(self):
        self.assertEqual(gen.GenParams(prompt="x", seed=10, batch=3).seeds(), [10, 11, 12])
        self.assertEqual(gen.GenParams(prompt="x", seed=2 ** 32 - 1, batch=2).seeds(), [2 ** 32 - 1, 0])
        a = gen.GenParams(prompt="x").seeds()
        self.assertEqual(len(a), 1)


class TestGuardado(unittest.TestCase):
    def test_guarda_prompt_y_semilla_dentro_del_png(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(gen, "OUTPUTS_DIR", Path(d)):
                p = gen.GenParams(prompt="un gato astronauta", negative="borroso", steps=20, cfg=6.5, seed=7)
                path = gen.save_png(Image.new("RGB", (8, 8)), p, 7, "sd15")
            with Image.open(path) as im:
                self.assertEqual(im.text["prompt"], "un gato astronauta")
                self.assertEqual(im.text["negative"], "borroso")
                self.assertIn("seed=7", im.text["settings"])
                self.assertIn("model=sd15", im.text["settings"])

    def test_no_pisa_archivos_existentes(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(gen, "OUTPUTS_DIR", Path(d)), mock.patch.object(gen.time, "strftime", return_value="T"):
                p = gen.GenParams(prompt="x")
                a = gen.save_png(Image.new("RGB", (4, 4)), p, 1, "sd15")
                b = gen.save_png(Image.new("RGB", (4, 4)), p, 1, "sd15")
            self.assertNotEqual(a, b)


class TestGpuReal(unittest.TestCase):
    def test_genera_una_imagen_de_verdad(self):
        if not mdl.is_installed(SD15):
            self.skipTest("SD 1.5 no está descargado")
        try:
            import torch
        except Exception:
            self.skipTest("PyTorch no está instalado")
        if not torch.cuda.is_available():
            self.skipTest("PyTorch no ve CUDA")
        from local_ai_studio.core.model_manager import ModelManager
        cfg = {"ollama": {"host": "http://localhost:11434"}}
        g = gen.ImageGenerator(ModelManager(cfg))
        try:
            with mock.patch.object(gen, "OUTPUTS_DIR", Path(tempfile.mkdtemp())):
                res = g.generate("sd15", gen.GenParams(prompt="a red apple on a table, photo", steps=12, seed=1))
            self.assertEqual(res[0].image.size, (512, 512))
            px = res[0].image.convert("L").resize((16, 16))
            vals = list(px.tobytes())
            self.assertGreater(max(vals) - min(vals), 20, "la imagen salió plana (¿negra?)")
        finally:
            g.unload()


if __name__ == "__main__":
    unittest.main()
