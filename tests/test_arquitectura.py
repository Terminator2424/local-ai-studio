"""Reglas de capas entre paquetes: `core` es la base, `ui` va arriba y no hay ciclos. Ver docs/ARQUITECTURA.md."""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PKG = ROOT / "local_ai_studio"

# paquete -> paquetes de los que PUEDE depender (además de sí mismo)
PERMITIDO = {
    "core": set(),
    "pdf": {"core"},
    "chat": {"core"},
    "images": {"core"},
    "agent": {"core", "pdf"},
    "rag": {"core", "pdf"},
    "study": {"core", "pdf"},
    "ui": {"core", "pdf", "chat", "images", "agent", "rag", "study"},
}


def dependencias() -> dict[str, dict[str, list[str]]]:
    """paquete -> {paquete del que depende: [archivos que lo importan]} (importaciones relativas, también las de dentro de funciones)."""
    deps: dict[str, dict[str, list[str]]] = {name: {} for name in PERMITIDO}
    for path in PKG.rglob("*.py"):
        rel = path.relative_to(PKG)
        if len(rel.parts) < 2:
            continue                                   # __init__ y __main__ de la raíz: pueden importar de cualquiera
        owner = rel.parts[0]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 2 and node.module:       # «from ..otro import x»
                target = node.module.split(".")[0]
                if target in PERMITIDO and target != owner:
                    deps[owner].setdefault(target, []).append(str(rel))
    return deps


class TestCapas(unittest.TestCase):
    def test_cada_paquete_solo_depende_de_lo_permitido(self):
        for owner, targets in dependencias().items():
            for target, files in targets.items():
                self.assertIn(target, PERMITIDO[owner], f"«{owner}» no debe importar de «{target}»: {files}")

    def test_no_hay_ciclos_entre_paquetes(self):
        deps = {k: set(v) for k, v in dependencias().items()}

        def visita(nodo: str, camino: list[str]):
            self.assertNotIn(nodo, camino, "ciclo: " + " → ".join([*camino, nodo]))
            for sig in deps.get(nodo, ()):
                visita(sig, [*camino, nodo])

        for nombre in deps:
            visita(nombre, [])

    def test_el_nucleo_no_conoce_la_interfaz_ni_los_demas(self):
        self.assertEqual(dependencias()["core"], {})

    def test_solo_la_interfaz_importa_customtkinter(self):
        for path in PKG.rglob("*.py"):
            if path.relative_to(PKG).parts[0] == "ui":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("import customtkinter", text, str(path.relative_to(ROOT)))
            self.assertNotIn("import tkinter", text, str(path.relative_to(ROOT)))

    def test_cada_paquete_declarado_existe(self):
        for name in PERMITIDO:
            self.assertTrue((PKG / name).is_dir(), name)
        carpetas = {p.name for p in PKG.iterdir() if p.is_dir() and p.name != "__pycache__"}
        self.assertEqual(carpetas, set(PERMITIDO), "un paquete nuevo debe añadirse a PERMITIDO (y a docs/ARQUITECTURA.md)")


if __name__ == "__main__":
    unittest.main()
