"""Asistente de primer arranque: dice qué falta y cómo arreglarlo, sin instalar nada."""
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_ai_studio import __main__ as cli
from local_ai_studio import firstrun

CFG = {"ollama": {"host": "http://127.0.0.1:11434"}}
TODO = {"llama3.1:8b", "qwen2.5vl:7b", "nomic-embed-text:latest"}
CUDA = {"version": "2.11.0+cu128", "cuda": True}


def by_title(steps, start):
    return next(s for s in steps if s.title.startswith(start))


class TestComprobaciones(unittest.TestCase):
    def test_todo_instalado_todo_bien(self):
        steps = firstrun.check_setup(CFG, torch=CUDA, installed=TODO)
        self.assertTrue(all(s.ok for s in steps), firstrun.format_steps(steps))
        self.assertEqual(firstrun.essentials_missing(steps), [])

    def test_ollama_sin_responder_es_imprescindible_y_explica_que_hacer(self):
        with mock.patch.object(firstrun, "get_client", side_effect=ConnectionError("apagado")):
            steps = firstrun.check_setup(CFG, torch=CUDA)
        missing = firstrun.essentials_missing(steps)
        self.assertEqual([s.title for s in missing][:1], ["Ollama"])
        self.assertIn("https://ollama.com", missing[0].fix)
        self.assertTrue(any(s.title == "Modelo de chat" and not s.ok for s in steps))     # y sin Ollama tampoco hay modelos

    def test_sin_modelo_de_chat_se_da_el_comando(self):
        steps = firstrun.check_setup(CFG, torch=CUDA, installed={"qwen2.5vl:7b"})            # solo hay modelo de visión
        chat = by_title(steps, "Modelo de chat")
        self.assertFalse(chat.ok)
        self.assertTrue(chat.essential)
        self.assertIn("ollama pull llama3.1:8b", chat.fix)

    def test_lo_opcional_no_es_imprescindible(self):
        steps = firstrun.check_setup(CFG, torch={"version": "2.11.0", "cuda": False}, installed={"llama3.1:8b"})
        self.assertEqual(firstrun.essentials_missing(steps), [])
        for start in ("Modelo de visión", "Búsqueda por sentido", "PyTorch con CUDA"):
            s = by_title(steps, start)
            self.assertFalse(s.ok)
            self.assertFalse(s.essential)
            self.assertTrue(s.fix)

    def test_pytorch_sin_cuda_se_dice_claro(self):
        steps = firstrun.check_setup(CFG, torch={"version": "2.11.0", "cuda": False}, installed=TODO)
        torch = by_title(steps, "PyTorch")
        self.assertIn("SIN CUDA", torch.detail)
        self.assertIn("download.pytorch.org", torch.fix)

    def test_un_modelo_de_vision_solo_no_cuenta_como_modelo_de_chat(self):
        chat = by_title(firstrun.check_setup(CFG, installed={"qwen2.5vl:7b"}), "Modelo de chat")
        self.assertFalse(chat.ok)

    def test_el_texto_marca_cada_estado(self):
        text = firstrun.format_steps(firstrun.check_setup(CFG, torch=CUDA, installed={"llama3.1:8b"}))
        self.assertIn("✔ Ollama", text)
        self.assertIn("○ Modelo de visión", text)               # opcional que falta: círculo, no cruz
        self.assertIn("    → En una terminal: ollama pull qwen2.5vl:7b", text)
        text2 = firstrun.format_steps(firstrun.check_setup(CFG, torch=CUDA, installed=set()))
        self.assertIn("✖ Modelo de chat", text2)                # imprescindible que falta: cruz


class TestDoctor(unittest.TestCase):
    def run_doctor(self, steps, required=()):
        out = io.StringIO()
        with mock.patch.object(firstrun, "check_setup", return_value=steps), \
                mock.patch.object(cli, "missing_dependencies", return_value=(list(required), [])), \
                contextlib.redirect_stdout(out):
            code = cli.doctor()
        return code, out.getvalue()

    def test_codigo_de_salida_cero_si_todo_va(self):
        code, text = self.run_doctor(firstrun.check_setup(CFG, torch=CUDA, installed=TODO))
        self.assertEqual(code, 0)
        self.assertIn("Primeros pasos", text)

    def test_codigo_de_salida_uno_si_falta_algo_imprescindible(self):
        code, _ = self.run_doctor(firstrun.check_setup(CFG, torch=CUDA, installed=set()))
        self.assertEqual(code, 1)

    def test_codigo_de_salida_uno_si_falta_una_dependencia(self):
        code, _ = self.run_doctor(firstrun.check_setup(CFG, torch=CUDA, installed=TODO), required=["sympy"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
