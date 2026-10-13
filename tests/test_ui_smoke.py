"""Prueba de humo de la interfaz: abre la ventana real y todas las páginas en un proceso aparte, con los datos en una carpeta
temporal (LOCAL_AI_STUDIO_HOME) para no tocar los del usuario. Se omite si no hay pantalla."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = r'''
import sys
sys.path.insert(0, sys.argv[1])
try:
    import tkinter
    tkinter.Tk().destroy()
except Exception:
    print("NO_DISPLAY"); sys.exit(3)
from local_ai_studio.ui.app import PAGES, StudioApp

app = StudioApp()
for key in PAGES:
    app.show(key)
    app.update()
    assert app.active == key, key
    assert key in app.pages, key
# comportamientos que dependen de varias piezas a la vez
chat = app.pages["chat"]
assert chat.model_var.get() == "llama3.1:8b", chat.model_var.get()
chat.model_var.set("qwen2.5-coder:7b"); assert chat.picker.get() == "qwen2.5-coder:7b"
chat._on_role("Programador"); assert "ingeniero de software" in chat.system_box.get("1.0", "end")
chat.system_box.insert("end", " extra"); chat._sync_role(); assert chat.role_var.get() == "Personalizado"
docs = app.pages["docs"]; assert docs.model_var.get() == "llama3.1:8b"
study = app.pages["study"]; assert study.model_var.get() == "llama3.1:8b"
app.pages["settings"]._show_storage([])
app.pages["settings"]._show_network()
assert "Activa" in app.pages["settings"].net_lbl.cget("text"), app.pages["settings"].net_lbl.cget("text")
# protocolo de la GPU: con un trabajo en curso, el resto se niega con un aviso y NO guarda ni arranca nada
from tkinter import messagebox
avisos = []
messagebox.showinfo = lambda title, msg, **k: avisos.append((title, msg))
assert app.manager.begin_job("Resumir PDF") is None
assert app.begin_job("Generar imagen") is False and "Resumir PDF" in avisos[-1][1], avisos
chat.input.delete("1.0", "end"); chat.input.insert("1.0", "hola")
n_antes = len(avisos); chat._send()
assert len(avisos) == n_antes + 1 and chat.input.get("1.0", "end").strip() == "hola", "el chat debe conservar el texto"
assert not chat.streaming and chat.conv_id is None, "no debe crear conversación"
docs._start_index([])
assert docs.working is False
app._tick_status(); assert "Trabajando: Resumir PDF" in app.status_lbl.cget("text"), app.status_lbl.cget("text")
app.end_job("Resumir PDF")
assert app.begin_job("Generar imagen") is True
app.end_job("Generar imagen")
assert app.manager.current_job is None
# primer arranque: si falta algo imprescindible avisa con el comando exacto y abre Ajustes
import local_ai_studio.ui.app as app_module
from local_ai_studio.firstrun import Step
app.show("pdf")
original = app_module.check_setup
app_module.check_setup = lambda cfg, **k: [Step("Modelo de chat", False, "ninguno instalado", "En una terminal: ollama pull llama3.1:8b", True)]
avisos.clear(); app._first_run_check()
assert avisos and "ollama pull llama3.1:8b" in avisos[-1][1] and app.active == "settings", (avisos, app.active)
app_module.check_setup = lambda cfg, **k: [Step("Ollama", True, "responde", essential=True)]
avisos.clear(); app.show("pdf"); app._first_run_check()
assert not avisos and app.active == "pdf", "sin carencias no debe molestar"
app_module.check_setup = original
app._toggle_dark(); app.update(); app._toggle_dark(); app.update()
app.destroy()
print("OK", len(PAGES))
'''


class TestInterfaz(unittest.TestCase):
    def test_se_abren_todas_las_paginas_sin_errores(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "LOCAL_AI_STUDIO_HOME": home, "PYTHONIOENCODING": "utf-8", "LOCAL_AI_STUDIO_SKIP_FIRSTRUN": "1"}
            r = subprocess.run([sys.executable, "-c", SCRIPT, str(ROOT)], env=env, capture_output=True, text=True,
                               timeout=180, check=False, encoding="utf-8", errors="replace")
            if r.returncode == 3 and "NO_DISPLAY" in r.stdout:
                self.skipTest("no hay pantalla disponible")
            real = [ln for ln in r.stderr.splitlines() if "invalid command name" not in ln and "while executing" not in ln
                    and 'after" script' not in ln and not ln.startswith('"') and ln.strip()]
            self.assertEqual(r.returncode, 0, f"stdout:\n{r.stdout}\nstderr:\n" + "\n".join(real))
            self.assertIn("OK 7", r.stdout)
            self.assertEqual([ln for ln in real if "Traceback" in ln or "Error" in ln], [], "\n".join(real))
            # los datos de la prueba quedaron en la carpeta temporal, no en el proyecto
            self.assertTrue((Path(home) / "logs" / "app.log").exists() or (Path(home) / "resumenes").exists())


if __name__ == "__main__":
    unittest.main()
