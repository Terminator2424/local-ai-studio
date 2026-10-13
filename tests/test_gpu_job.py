"""Protocolo de la GPU: con 8 GB solo cabe un trabajo pesado a la vez (resumir, generar imagen, chatear…)."""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local_ai_studio.core.model_manager import ModelManager


class TestTrabajoPesado(unittest.TestCase):
    def setUp(self):
        self.m = ModelManager({"ollama": {}})

    def test_libre_al_empezar(self):
        self.assertIsNone(self.m.current_job)

    def test_el_primero_la_consigue_y_el_segundo_sabe_quien_la_tiene(self):
        self.assertIsNone(self.m.begin_job("Resumir PDF"))
        self.assertEqual(self.m.current_job, "Resumir PDF")
        self.assertEqual(self.m.begin_job("Generar imagen"), "Resumir PDF")
        self.assertEqual(self.m.current_job, "Resumir PDF")             # el segundo no la pisa

    def test_al_terminar_queda_libre_para_otro(self):
        self.m.begin_job("Resumir PDF")
        self.m.end_job("Resumir PDF")
        self.assertIsNone(self.m.begin_job("Generar imagen"))

    def test_un_fallo_de_otro_no_libera_el_trabajo_en_curso(self):
        self.m.begin_job("Resumir PDF")
        self.m.end_job("Generar imagen")                                 # lo intenta quien nunca la tuvo
        self.assertEqual(self.m.current_job, "Resumir PDF")

    def test_terminar_dos_veces_no_rompe_nada(self):
        self.m.begin_job("Chat")
        self.m.end_job("Chat")
        self.m.end_job("Chat")
        self.assertIsNone(self.m.current_job)

    def test_carrera_real_entre_hilos_solo_uno_gana(self):
        """Veinte hilos piden la GPU a la vez: exactamente uno la consigue."""
        ganadores, barrera = [], threading.Barrier(20)

        def pide(n: int):
            barrera.wait()
            if self.m.begin_job(f"trabajo {n}") is None:
                ganadores.append(n)

        hilos = [threading.Thread(target=pide, args=(n,)) for n in range(20)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        self.assertEqual(len(ganadores), 1)
        self.assertEqual(self.m.current_job, f"trabajo {ganadores[0]}")


if __name__ == "__main__":
    unittest.main()
