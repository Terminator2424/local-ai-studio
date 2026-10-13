"""Pruebas del troceo de fragmentos (local_ai_studio/chunking.py), sin dependencias externas."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from types import SimpleNamespace

from local_ai_studio.pdf.chunking import RecursiveSplitter, build_chunks

SEPS = ["\n\n\n", "\n\n", "\n", ". ", "; ", ", ", " ", ""]


class TestRecursiveSplitter(unittest.TestCase):
    def setUp(self):
        self.text = ("Párrafo uno con varias palabras para llenar el fragmento.\n\n" * 4
                     + "Una frase larga, con comas, y punto. " * 12)

    def test_ningun_fragmento_supera_el_tamano(self):
        docs = RecursiveSplitter(chunk_size=120, chunk_overlap=30, separators=SEPS).create_documents([self.text])
        self.assertTrue(docs)
        self.assertLessEqual(max(len(d.page_content) for d in docs), 120)

    def test_start_index_apunta_al_texto_original(self):
        docs = RecursiveSplitter(chunk_size=120, chunk_overlap=30, separators=SEPS).create_documents([self.text])
        for d in docs:
            start = d.metadata["start_index"]
            self.assertEqual(self.text[start:start + 25], d.page_content[:25])

    def test_cubre_todo_el_texto(self):
        docs = RecursiveSplitter(chunk_size=120, chunk_overlap=30, separators=SEPS).create_documents([self.text])
        joined = " ".join(d.page_content for d in docs)
        for palabra in ["Párrafo", "fragmento.", "Una frase", "punto."]:
            self.assertIn(palabra, joined)

    def test_hay_solape_entre_fragmentos_consecutivos(self):
        # Con trozos pequeños (palabras) el solape sí cabe: el fragmento siguiente empieza antes de que acabe el anterior
        words = "palabra " * 60
        docs = RecursiveSplitter(chunk_size=50, chunk_overlap=20, separators=SEPS).create_documents([words])
        self.assertGreaterEqual(len(docs), 3)
        self.assertLess(docs[1].metadata["start_index"], docs[0].metadata["start_index"] + len(docs[0].page_content))

    def test_texto_corto_queda_en_un_fragmento(self):
        docs = RecursiveSplitter(chunk_size=500, chunk_overlap=0, separators=SEPS).create_documents(["Hola mundo."])
        self.assertEqual([d.page_content for d in docs], ["Hola mundo."])

    def test_build_chunks_funciona_con_paginas_sinteticas(self):
        page = SimpleNamespace(number=1, text="Problema 1. Enunciado corto.\n\n" + "x " * 400, eqs=[])
        chunks = build_chunks([page], 300, 40, SEPS)
        self.assertTrue(chunks)


if __name__ == "__main__":
    unittest.main()
