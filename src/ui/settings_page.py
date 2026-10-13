"""Página «Ajustes / Diagnóstico»: GPU, VRAM, PyTorch/CUDA, modelos de Ollama e imágenes, y control de lo cargado.

Las comprobaciones lentas (importar torch, listar modelos) corren en un hilo; las descargas solo empiezan si lo confirmas.
"""
from __future__ import annotations

import logging
import queue
import threading
from tkinter import messagebox

import customtkinter as ctk

from ..chat.engine import CHAT_MODELS, ChatEngine
from ..core import hardware_detector as hw
from ..images import models as mdl
from .theme import ACCENT, BORDER, ERR, MUTED, OK, SURFACE, TEXT, WARN, font, ghost_button, label, primary_button

log = logging.getLogger("resumidor")
EXTRA_MODELS = {"nomic-embed-text": "Embeddings para «Chat con mis documentos» (≈ 270 MB)"}


def torch_report() -> dict:
    """Estado de PyTorch/CUDA. Importa torch, así que se llama desde un hilo."""
    try:
        import torch
        cuda = torch.cuda.is_available()
        return {"version": torch.__version__, "cuda": cuda, "cuda_version": torch.version.cuda,
                "device": torch.cuda.get_device_name(0) if cuda else None}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


class SettingsPage(ctk.CTkFrame):
    title = "Ajustes"

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.cfg = app.cfg
        self.q: queue.Queue = queue.Queue()
        self.engine = ChatEngine(self.cfg)
        self.manager = app.manager
        self.pulling = False
        self.report_text = ""
        self.cancel = threading.Event()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent", scrollbar_button_color=BORDER)
        self.scroll.grid(row=0, column=0, sticky="nsew")
        self.after(100, self._poll)
        self._build()
        self.refresh()

    def busy(self) -> bool:
        return self.pulling

    def shutdown(self):
        self.cancel.set()

    def on_theme(self):
        pass

    def on_show(self):
        self.refresh()

    # ----------------------------------------------------------------- UI
    def _card(self, title: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self.scroll, fg_color=SURFACE, corner_radius=12, border_width=1, border_color=BORDER)
        card.pack(fill="x", pady=(0, 12), padx=2)
        label(card, title, 14, "bold").pack(anchor="w", padx=16, pady=(12, 4))
        return card

    def _build(self):
        head = ctk.CTkFrame(self.scroll, fg_color="transparent")
        head.pack(fill="x", pady=(0, 10))
        label(head, "Diagnóstico", 20, "bold").pack(side="left")
        ghost_button(head, "Copiar informe", self._copy, width=130, height=30).pack(side="right")
        ghost_button(head, "Actualizar", self.refresh, width=100, height=30).pack(side="right", padx=8)

        self.hw_card = self._card("GPU y memoria")
        self.hw_lbl = label(self.hw_card, "", 13, justify="left", anchor="w", wraplength=900)
        self.hw_lbl.pack(anchor="w", padx=16, pady=(0, 12), fill="x")

        self.cfg_card = self._card("Configuración aplicada (automática según tu hardware)")
        self.cfg_lbl = label(self.cfg_card, "", 13, justify="left", anchor="w", wraplength=900)
        self.cfg_lbl.pack(anchor="w", padx=16, pady=(0, 12), fill="x")

        self.torch_card = self._card("PyTorch (imágenes)")
        self.torch_lbl = label(self.torch_card, "Comprobando…", 13, justify="left", anchor="w", wraplength=900)
        self.torch_lbl.pack(anchor="w", padx=16, pady=(0, 12), fill="x")

        self.mem_card = self._card("En memoria ahora")
        self.mem_lbl = label(self.mem_card, "", 13, justify="left", anchor="w", wraplength=900)
        self.mem_lbl.pack(anchor="w", padx=16, pady=(0, 6), fill="x")
        ghost_button(self.mem_card, "Liberar toda la VRAM", self._free, width=170, height=30).pack(anchor="w", padx=16, pady=(0, 12))

        self.llm_card = self._card("Modelos de chat (Ollama, solo en 127.0.0.1)")
        self.llm_rows = ctk.CTkFrame(self.llm_card, fg_color="transparent")
        self.llm_rows.pack(fill="x", padx=16, pady=(0, 6))
        self.pull_lbl = label(self.llm_card, "", 12, muted=True)
        self.pull_lbl.pack(anchor="w", padx=16, pady=(0, 10))

        self.img_card = self._card("Modelos de imágenes")
        self.img_rows = ctk.CTkFrame(self.img_card, fg_color="transparent")
        self.img_rows.pack(fill="x", padx=16, pady=(0, 12))

    # ------------------------------------------------------------ refrescar
    def refresh(self):
        threading.Thread(target=self._gather, daemon=True).start()

    def _gather(self):
        try:
            info = hw.detect(force=True)
            installed = self.engine.installed()
            running = self.manager.running_llms()
            data = {"info": info, "installed": installed, "running": running,
                    "cfg": hw.describe(self.cfg)}
        except Exception as exc:  # noqa: BLE001
            data = {"error": str(exc)}
        self.q.put(("data", data))
        self.q.put(("torch", torch_report()))

    def _poll(self):
        try:
            for _ in range(20):
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "data":
            self._show_data(msg[1])
        elif kind == "torch":
            r = msg[1]
            if "error" in r:
                txt, col = f"PyTorch no se pudo cargar: {r['error']}", ERR
            elif r["cuda"]:
                txt, col = f"PyTorch {r['version']} con CUDA {r['cuda_version']} · GPU: {r['device']}", OK
            else:
                txt, col = (f"PyTorch {r['version']} SIN CUDA: no se pueden generar imágenes en GPU. "
                            "Instala la build CUDA (ver README, «Instalación para imágenes»).", WARN)
            self.torch_lbl.configure(text=txt, text_color=col)
            self.torch_text = txt
        elif kind == "pull":
            self.pull_lbl.configure(text=msg[1])
        elif kind == "pull_done":
            self.pulling = False
            self.pull_lbl.configure(text=f"✔ {msg[1]} descargado")
            self.refresh()
        elif kind == "pull_error":
            self.pulling = False
            self.pull_lbl.configure(text=f"✖ {msg[1]}")

    def _show_data(self, d):
        if "error" in d:
            self.hw_lbl.configure(text=f"No se pudo leer el hardware: {d['error']}", text_color=ERR)
            return
        i = d["info"]
        lines = [f"GPU: {i.gpu_name}", f"VRAM: {i.vram_free_gb:.1f} GB libres de {i.vram_total_gb:.1f} GB",
                 f"RAM: {i.ram_gb:.0f} GB", *i.notes]
        self.hw_lbl.configure(text="\n".join(lines), text_color=TEXT)
        self.cfg_lbl.configure(text=d["cfg"], text_color=TEXT)
        running = d["running"]
        state = self.manager.state
        self.mem_lbl.configure(text=f"Gestor: {state}\nOllama tiene cargado: {', '.join(running) if running else 'nada'}")
        self.report_text = "\n".join(["== Local AI Studio: diagnóstico ==", *lines, d["cfg"], f"En memoria: {state}",
                                      f"Torch: {getattr(self, 'torch_text', 'comprobando')}",
                                      "Modelos Ollama: " + ", ".join(sorted(d["installed"]))])
        for w in self.llm_rows.winfo_children():
            w.destroy()
        for name, note in {**CHAT_MODELS, **EXTRA_MODELS}.items():
            row = ctk.CTkFrame(self.llm_rows, fg_color="transparent")
            row.pack(fill="x", pady=2)
            have = any(m == name or m == name + ":latest" for m in d["installed"])
            label(row, ("✔ " if have else "○ ") + name, 13, "bold" if have else "normal").pack(side="left")
            label(row, "  " + note, 12, muted=True).pack(side="left")
            if not have:
                ghost_button(row, "Descargar…", lambda n=name: self._pull(n), width=100, height=26).pack(side="right")
        for w in self.img_rows.winfo_children():
            w.destroy()
        for m in mdl.CATALOG.values():
            row = ctk.CTkFrame(self.img_rows, fg_color="transparent")
            row.pack(fill="x", pady=2)
            have = mdl.is_installed(m)
            label(row, ("✔ " if have else "○ ") + m.title, 13, "bold" if have else "normal").pack(side="left")
            label(row, f"  ≈ {m.size_mb / 1000:.1f} GB · {m.license}", 12, muted=True).pack(side="left")
            if have:
                ghost_button(row, "Verificar hashes", lambda m=m: self._verify(m), width=130, height=26).pack(side="right")
            else:
                label(row, "descárgalo desde «Generar imagen»", 12, muted=True).pack(side="right")

    # ------------------------------------------------------------- acciones
    def _pull(self, name: str):
        if self.pulling:
            return
        if not messagebox.askyesno("Descargar modelo", f"Se descargará «{name}» desde ollama.com (varios GB).\n\n"
                                   "Es lo único que se descarga; tus datos no salen del equipo.\n\n¿Continuar?"):
            return
        self.pulling = True
        self.cancel.clear()
        threading.Thread(target=self._pull_worker, args=(name,), daemon=True).start()

    def _pull_worker(self, name: str):
        try:
            self.engine.pull(name, lambda f, s: self.q.put(("pull", f"{name}: {s}" + (f" {f * 100:.0f} %" if f else ""))),
                             self.cancel)
            self.q.put(("pull_done", name))
        except Exception as exc:  # noqa: BLE001
            self.q.put(("pull_error", f"{type(exc).__name__}: {exc}"))

    def _verify(self, m):
        def work():
            bad = mdl.verify(m)
            self.q.put(("pull", ("✔ Hashes correctos: " if not bad else "✖ NO coinciden: ") + (m.title if not bad else ", ".join(bad))))
        threading.Thread(target=work, daemon=True).start()
        self.pull_lbl.configure(text=f"Verificando {m.title}… (puede tardar)")

    def _free(self):
        def work():
            try:
                self.manager.free_all()
            finally:
                self.q.put(("pull", "✔ VRAM liberada"))
                self.refresh()
        threading.Thread(target=work, daemon=True).start()

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.report_text)
