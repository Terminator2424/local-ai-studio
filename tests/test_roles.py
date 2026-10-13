"""Pruebas de los roles predefinidos del chat."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_ai_studio.chat import engine, roles
from local_ai_studio.chat.store import DEFAULT_PREFS


class TestRoles(unittest.TestCase):
    def test_hay_roles_con_descripcion_y_prompt_distintos(self):
        self.assertGreaterEqual(len(roles.ROLES), 5)
        prompts = [p for _, p in roles.ROLES.values()]
        self.assertEqual(len(prompts), len(set(prompts)))
        for name, (desc, prompt) in roles.ROLES.items():
            self.assertTrue(desc.strip() and len(prompt) > 60, name)

    def test_el_rol_predeterminado_es_el_prompt_de_siempre(self):
        self.assertEqual(roles.prompt_for(roles.DEFAULT_ROLE), DEFAULT_PREFS["system_prompt"])
        self.assertEqual(engine.DEFAULT_SYSTEM, DEFAULT_PREFS["system_prompt"])
        self.assertEqual(roles.role_names()[0], roles.DEFAULT_ROLE)
        self.assertEqual(roles.role_names()[-1], roles.CUSTOM)

    def test_el_texto_decide_el_rol(self):
        for name in roles.ROLES:
            self.assertEqual(roles.role_for(roles.prompt_for(name)), name)
            self.assertEqual(roles.role_for("  " + roles.prompt_for(name) + "\n\n"), name)        # espacios de más no importan
        self.assertEqual(roles.role_for("Eres un pirata."), roles.CUSTOM)
        self.assertEqual(roles.role_for(""), roles.CUSTOM)
        self.assertIsNone(roles.prompt_for(roles.CUSTOM))
        self.assertIsNone(roles.prompt_for("no existe"))

    def test_todos_los_roles_exigen_fidelidad(self):
        """Ningún rol invita a inventar: todos piden decir cuándo no se sabe o no inventar datos."""
        for name, (_, prompt) in roles.ROLES.items():
            low = prompt.lower()
            self.assertTrue("no estás seguro" in low or "no inventes" in low, name)

    def test_el_rol_de_seguridad_es_defensivo_y_exige_autorizacion(self):
        prompt = roles.prompt_for("Seguridad (defensiva)").lower()
        for must in ("defensiva", "corrección", "autoriz"):
            self.assertIn(must, prompt)

    def test_descripciones(self):
        self.assertTrue(roles.describe("Programador"))
        self.assertIn("propio", roles.describe(roles.CUSTOM))


if __name__ == "__main__":
    unittest.main()
