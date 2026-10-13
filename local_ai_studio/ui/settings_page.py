"""Página «Ajustes / Diagnóstico»: GPU, VRAM, PyTorch/CUDA, modelos de Ollama e imágenes, y control de lo cargado.

Las comprobaciones lentas (importar torch, listar modelos) corren en un hilo; las descargas solo empiezan si lo confirmas.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
from tkinter import messagebox

import customtkinter as ctk

from ..chat.engine import MODEL_INFO, ChatEngine, model_detail, model_label
from ..core import hardware_detector as hw
from ..core import netguard, storage
from ..firstrun import check_setup, essentials_missing, format_steps
from ..images import models as mdl
from .theme import BORDER, ERR, OK, SURFACE, TEXT, WARN, ghost_button, label

log = logging.getLogger(__name__)
EXTRA_MODELS = {"nomic-embed-text": "Búsqueda por sentido en «Documentos» (≈ 270 MB)"}


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

        self.setup_card = self._card("Primeros pasos")
        self.setup_lbl = label(self.setup_card, "Comprobando…", 13, justify="left", anchor="w", wraplength=900)
        self.setup_lbl.pack(anchor="w", padx=16, pady=(0, 12), fill="x")

        self.hw_card = self._card("GPU y memoria")
        self.hw_lbl = label(self.hw_card, "Leyendo el hardware…", 13, justify="left", anchor="w", wraplength=900)
        self.hw_lbl.pack(anchor="w", padx=16, pady=(0, 12), fill="x")

        self.cfg_card = self._card("Configuración aplicada (automática según tu hardware)")
        self.cfg_lbl = label(self.cfg_card, "Leyendo…", 13, justify="left", anchor="w", wraplength=900)
        self.cfg_lbl.pack(anchor="w", padx=16, pady=(0, 12), fill="x")

        self.torch_card = self._card("PyTorch (imágenes)")
        self.torch_lbl = label(self.torch_card, "Comprobando…", 13, justify="left", anchor="w", wraplength=900)
        self.torch_lbl.pack(anchor="w", padx=16, pady=(0, 12), fill="x")

        self.mem_card = self._card("En memoria ahora")
        self.mem_lbl = label(self.mem_card, "Leyendo…", 13, justify="left", anchor="w", wraplength=900)
        self.mem_lbl.pack(anchor="w", padx=16, pady=(0, 6), fill="x")
        ghost_button(self.mem_card, "Liberar toda la VRAM", self._free, width=170, height=30).pack(anchor="w", padx=16, pady=(0, 12))

        self.net_card = self._card("Barrera de red")
        self.net_lbl = label(self.net_card, "", 13, justify="left", anchor="w", wraplength=900)
        self.net_lbl.pack(anchor="w", padx=16, pady=(0, 12), fill="x")

        self.llm_card = self._card("Modelos de chat (Ollama, solo en 127.0.0.1)")
        self.llm_rows = ctk.CTkFrame(self.llm_card, fg_color="transparent")
        self.llm_rows.pack(fill="x", padx=16, pady=(0, 6))
        self.pull_lbl = label(self.llm_card, "", 12, muted=True)
        self.pull_lbl.pack(anchor="w", padx=16, pady=(0, 10))

        self.img_card = self._card("Modelos de imágenes")
        self.img_rows = ctk.CTkFrame(self.img_card, fg_color="transparent")
        self.img_rows.pack(fill="x", padx=16, pady=(0, 12))

        self.disk_card = self._card("Almacenamiento")
        self.disk_rows = ctk.CTkFrame(self.disk_card, fg_color="transparent")
        self.disk_rows.pack(fill="x", padx=16, pady=(0, 6))
        label(self.disk_card, "Solo se pueden limpiar la caché y las páginas renderizadas: se vuelven a crear solas. "
              "Tus resúmenes, chats, documentos e imágenes nunca se borran desde aquí.", 11, muted=True, wraplength=900,
              justify="left").pack(anchor="w", padx=16, pady=(0, 12))

    # ------------------------------------------------------------ refrescar
    def refresh(self):
        threading.Thread(target=self._gather, daemon=True).start()

    def _gather(self):
        try:
            info = hw.detect(force=True)
            installed = self.engine.installed()
            running = self.manager.running_llms()
            data = {"info": info, "installed": installed, "running": running,
                    "cfg": hw.describe(self.cfg), "storage": storage.usage()}
        except Exception as exc:  # noqa: BLE001
            data = {"error": str(exc)}
        self.q.put(("data", data))
        report = torch_report()
        self.q.put(("torch", report))
        if "installed" in data:
            self.q.put(("setup", check_setup(self.cfg, torch=report, installed=data["installed"])))

    def _poll(self):
        try:
            for _ in range(20):
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "setup":
            steps = msg[1]
            missing = essentials_missing(steps)
            self.setup_lbl.configure(text=format_steps(steps), text_color=ERR if missing else TEXT)
        elif kind == "data":
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
        self._show_network()
        running = d["running"]
        state = self.manager.state
        self.mem_lbl.configure(text=f"Gestor: {state}\nOllama tiene cargado: {', '.join(running) if running else 'nada'}")
        self.report_text = "\n".join(["== Local AI Studio: diagnóstico ==", *lines, d["cfg"], f"En memoria: {state}",
                                      f"Torch: {getattr(self, 'torch_text', 'comprobando')}",
                                      "Modelos Ollama: " + ", ".join(sorted(d["installed"]))])
        for w in self.llm_rows.winfo_children():
            w.destroy()
        for name in (*MODEL_INFO, *EXTRA_MODELS):
            row = ctk.CTkFrame(self.llm_rows, fg_color="transparent")
            row.pack(fill="x", pady=3)
            have = any(m == name or m == name + ":latest" for m in d["installed"])
            head = model_label(name) if name in MODEL_INFO else name
            label(row, ("✔ " if have else "○ ") + head, 13, "bold" if have else "normal").pack(anchor="w")
            label(row, "     " + (model_detail(name) or EXTRA_MODELS.get(name, "")), 11, muted=True, wraplength=760,
                  justify="left").pack(anchor="w")
            if not have:
                ghost_button(row, "Descargar…", lambda n=name: self._pull(n), width=100, height=26).place(relx=1.0, rely=0.0, anchor="ne")
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

        self._show_storage(d["storage"])

    def _show_network(self):
        """La barrera de red a la vista: activa o no, y los intentos de conexión externa que bloqueó."""
        if not netguard.is_active():
            self.net_lbl.configure(text="⚠ La barrera de red no está activa.", text_color=ERR)
            return
        blocked = netguard.blocked_attempts()
        text = ("✔ Activa: esta aplicación solo puede hablar con este equipo (127.0.0.1); cualquier conexión a Internet se aborta, "
                "salvo las descargas de modelos que confirmas.\n")
        text += (f"Intentos bloqueados desde que abriste la aplicación: {len(blocked)}"
                 + ("".join(f"\n  • {b}" for b in blocked[-5:])) if blocked else "Intentos bloqueados desde que abriste la aplicación: 0")
        self.net_lbl.configure(text=text, text_color=OK if not blocked else WARN)

    def _show_storage(self, rows):
        for w in self.disk_rows.winfo_children():
            w.destroy()
        for area, size in rows:
            row = ctk.CTkFrame(self.disk_rows, fg_color="transparent")
            row.pack(fill="x", pady=3)
            label(row, f"{area.title}: {storage.human(size)}", 13, "bold").pack(anchor="w")
            label(row, area.note, 11, muted=True, wraplength=640, justify="left").pack(anchor="w")
            bar = ctk.CTkFrame(row, fg_color="transparent")
            bar.place(relx=1.0, rely=0.0, anchor="ne")
            if area.clearable and size:
                ghost_button(bar, "Limpiar", lambda a=area, s=size: self._clear(a, s), width=80, height=26).pack(side="left", padx=(0, 6))
            ghost_button(bar, "Abrir carpeta", lambda a=area: self._open_folder(a), width=110, height=26).pack(side="left")

    # ------------------------------------------------------------- acciones
    def _clear(self, area, size: int):
        if not messagebox.askyesno("Limpiar", f"Se borrarán los {storage.human(size)} de «{area.title}».\n\n"
                                   "Son datos temporales: se volverán a crear cuando hagan falta.\n\n¿Limpiar?"):
            return

        def work():
            freed = storage.clear(area.key)
            self.q.put(("pull", f"✔ {area.title}: {storage.human(freed)} liberados"))
            self.refresh()
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _open_folder(area):
        area.path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(area.path))                # noqa: S606  (carpeta fija de la aplicación, no entrada del usuario)

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
        if self.app.job_busy():          # liberar la VRAM en pleno trabajo lo rompería
            return

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
