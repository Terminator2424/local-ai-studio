"""Higiene del repositorio: reglas que, rotas una vez, causaron fallos silenciosos."""
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PACKAGE = ROOT / "local_ai_studio"
SOURCES = sorted(PACKAGE.rglob("*.py")) + sorted((ROOT / "tests").glob("*.py")) + [ROOT / "app.py"]
CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")        # todo salvo \t, \n y \r


class TestCodigoFuente(unittest.TestCase):
    def test_ningun_archivo_tiene_caracteres_de_control(self):
        """Un «\\b» de una regex que se guarda como carácter backspace (0x08) no da error y rompe la regex en silencio."""
        for f in SOURCES:
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                self.assertIsNone(CONTROL.search(line), f"{f.relative_to(ROOT)}:{n} tiene un carácter de control: {line[:80]!r}")

    def test_todo_el_codigo_es_utf8_y_compila(self):
        for f in SOURCES:
            ast.parse(f.read_text(encoding="utf-8"), filename=str(f))

    def test_la_aplicacion_nunca_instala_paquetes_por_su_cuenta(self):
        for f in PACKAGE.rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            self.assertNotIn('"pip", "install"', text, f.name)
            self.assertNotIn("ensure_package", text, f.name)

    def test_los_loggers_son_por_modulo(self):
        for f in PACKAGE.rglob("*.py"):
            for m in re.finditer(r"getLogger\(([^)]*)\)", f.read_text(encoding="utf-8")):
                self.assertIn(m.group(1), ("__name__", '"local_ai_studio"'), f"{f.relative_to(ROOT)}: getLogger({m.group(1)})")

    def test_no_queda_ninguna_referencia_al_paquete_antiguo(self):
        for f in SOURCES:
            if f.name == "test_repo.py":
                continue
            self.assertNotRegex(f.read_text(encoding="utf-8"), r"\bfrom src\b|\bimport src\b", f.name)

    def test_sin_excepciones_vacias_en_el_nucleo_de_seguridad(self):
        """`except: pass` esconde errores: en agent/, core/netguard y chat/render no se admite."""
        for rel in ("agent/policy.py", "agent/tools.py", "agent/loop.py", "agent/sandbox.py", "core/netguard.py", "chat/render.py"):
            tree = ast.parse((PACKAGE / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    self.fail(f"{rel}:{node.lineno} traga una excepción sin registrarla")


class TestProyecto(unittest.TestCase):
    def test_la_version_del_paquete_tiene_formato_semantico(self):
        import local_ai_studio
        self.assertRegex(local_ai_studio.__version__, r"^\d+\.\d+\.\d+$")

    def test_los_documentos_esenciales_existen(self):
        for name in ("README.md", "SECURITY.md", "LICENSE", "pyproject.toml", "requirements.txt", "config.example.yaml",
                     ".gitignore", ".gitattributes", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "CHANGELOG.md"):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_los_datos_del_usuario_no_se_suben_a_git(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for entry in ("config.yaml", "conversations/", "outputs/", "models/", "workspace/", "logs/", "venv/", "*.safetensors"):
            self.assertIn(entry, ignored, entry)

    def test_los_scripts_bat_no_activan_el_entorno_virtual(self):
        """`activate.bat` guarda la ruta absoluta del entorno: al mover o renombrar la carpeta apunta a otro Python."""
        for name in ("run.bat", "build_exe.bat"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("call venv", text, name)       # «call venv\Scripts\activate.bat»
            self.assertIn("venv\\Scripts\\python.exe", text, name)

    def test_los_yaml_del_repositorio_son_validos(self):
        """Un YAML roto en .github/ hace que GitHub no arranque la CI ni diga por qué (0 trabajos): se valida aquí."""
        import yaml
        files = list((ROOT / ".github").rglob("*.yml")) + [ROOT / "config.example.yaml", ROOT / ".pre-commit-config.yaml"]
        self.assertTrue(files)
        for f in files:
            with self.subTest(archivo=str(f.relative_to(ROOT))):
                self.assertIsInstance(yaml.safe_load(f.read_text(encoding="utf-8")), dict)

    def test_la_ci_ejecuta_los_mismos_pasos_que_check_bat(self):
        import yaml
        ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        runs = " ".join(str(s.get("run", "")) for s in ci["jobs"]["calidad"]["steps"])
        for comando in ("ruff check", "mypy local_ai_studio", "unittest discover", "bandit"):
            self.assertIn(comando, runs, comando)


if __name__ == "__main__":
    unittest.main()
