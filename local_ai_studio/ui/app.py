"""Ventana principal de Local AI Studio: menú lateral y páginas (Resumir PDF, Chat, Imágenes, Ajustes).

Las páginas se crean la primera vez que se abren (arranque rápido, y un módulo opcional que falte —por ejemplo torch—
no impide usar el resto). Una barra de estado muestra qué modelo ocupa la VRAM y cuánta queda libre.
"""
from __future__ import annotations

import importlib
import logging
import os
import threading
from tkinter import messagebox

import customtkinter as ctk

from ..core import hardware_detector, netguard, storage
from ..core.config import load_config
from ..core.logsetup import setup_logging
from ..core.model_manager import get_manager
from ..core.ollama_client import close_clients
from ..firstrun import check_setup, essentials_missing
from . import theme
from .theme import ACCENT, BG, BORDER, MUTED, SURFACE, TEXT, font

try:  # arrastrar y soltar (opcional)
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:  # noqa: BLE001
    HAS_DND = False

log = logging.getLogger(__name__)

if HAS_DND:
    class _Base(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.TkdndVersion = TkinterDnD._require(self)
else:
    _Base = ctk.CTk  # type: ignore[misc]

# clave -> (texto del menú, módulo, clase)
PAGES = {
    "pdf": ("Resumir PDF", "pdf_page", "PdfPage"),
    "docs": ("Documentos", "docs_page", "DocsPage"),
    "chat": ("Chat", "chat_page", "ChatPage"),
    "study": ("Estudiar", "study_page", "StudyPage"),
    "gen": ("Generar imagen", "gen_page", "GenPage"),
    "edit": ("Editar imagen", "edit_page", "EditPage"),
    "audio": ("Transcribir Audio", "audio_page", "AudioPage"),
    "settings": ("Ajustes", "settings_page", "SettingsPage"),
}


class StudioApp(_Base):
    def __init__(self):
        super().__init__()
        setup_logging()
        netguard.install()          # nada de lo que ejecute esta ventana puede salir del equipo
        self.cfg = load_config()
        self.manager = get_manager(self.cfg)
        self.dark = theme.load_dark()
        self.has_dnd = HAS_DND
        self.pages: dict[str, ctk.CTkFrame] = {}
        self.active: str | None = None
        self._state_text = self.manager.state
        self.manager.listeners.append(self._on_manager_state)

        ctk.set_appearance_mode("dark" if self.dark else "light")
        ctk.set_default_color_theme("blue")
        self.title("Local AI Studio")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        sc = ctk.ScalingTracker.get_window_scaling(self)
        self.geometry(f"{min(1320, int(sw * 0.92 / sc))}x{min(780, int((sh - 120) * 0.9 / sc))}")
        self.minsize(1000, 640)
        self.configure(fg_color=BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        for key, name in (("<Control-o>", "open"), ("<Control-s>", "save"), ("<Control-Return>", "run")):
            self.bind(key, lambda e, n=name: self._shortcut(n))

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew", padx=(18, 22), pady=(16, 6))
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)
        self._build_status()
        self._setup_dnd()
        self.show("pdf")
        self.after(500, self._tick_status)
        if not os.environ.get("LOCAL_AI_STUDIO_SKIP_FIRSTRUN"):        # las pruebas automáticas lo desactivan (no hay Ollama en la CI)
            self.after(1800, self._first_run_check)
        threading.Thread(target=storage.auto_prune, daemon=True).start()      # mantiene la caché por debajo de su tope

    # ----------------------------------------------------------------- menú
    def _build_sidebar(self):
        side = ctk.CTkFrame(self, width=200, corner_radius=0, fg_color=SURFACE, border_width=0)
        side.grid(row=0, column=0, rowspan=2, sticky="nsw")
        side.grid_propagate(False)
        ctk.CTkLabel(side, text="Local AI Studio", font=font(17, "bold"), text_color=TEXT).pack(anchor="w", padx=20, pady=(22, 2))
        ctk.CTkLabel(side, text="100 % en tu equipo", font=font(11), text_color=MUTED).pack(anchor="w", padx=20, pady=(0, 20))
        self.nav: dict[str, ctk.CTkButton] = {}
        for key, (text, _, _) in PAGES.items():
            b = ctk.CTkButton(side, text=text, anchor="w", height=38, corner_radius=8, fg_color="transparent",
                              hover_color=BORDER, text_color=TEXT, font=font(14), command=lambda k=key: self.show(k))
            b.pack(fill="x", padx=12, pady=2)
            self.nav[key] = b
        self.dark_switch = ctk.CTkSwitch(side, text="Modo oscuro", command=self._toggle_dark, progress_color=ACCENT,
                                         font=font(12), text_color=MUTED)
        self.dark_switch.pack(side="bottom", anchor="w", padx=20, pady=20)
        if self.dark:
            self.dark_switch.select()

    def _build_status(self):
        bar = ctk.CTkFrame(self, fg_color="transparent", height=26)
        bar.grid(row=1, column=1, sticky="ew", padx=(18, 22), pady=(0, 8))
        bar.grid_columnconfigure(0, weight=1)
        self.status_lbl = ctk.CTkLabel(bar, text="", anchor="w", font=font(11), text_color=MUTED)
        self.status_lbl.grid(row=0, column=0, sticky="ew")

    def _on_manager_state(self, state: str):
        self._state_text = state            # se muestra en el siguiente ciclo del hilo principal (no se toca Tk desde otro hilo)

    def _first_run_check(self) -> None:
        """Si falta algo imprescindible (Ollama o un modelo de chat), lo dice una vez y abre Ajustes con las instrucciones."""
        try:
            missing = essentials_missing(check_setup(self.cfg))
        except Exception:
            log.exception("No se pudo comprobar el primer arranque")
            return
        if not missing:
            return
        text = "\n\n".join(f"• {s.title}: {s.detail}\n   {s.fix}" for s in missing)
        messagebox.showinfo("Faltan cosas para empezar", f"{text}\n\nTe lo dejo explicado en Ajustes → Primeros pasos.")
        self.show("settings")

    def job_busy(self) -> bool:
        """¿Hay un trabajo pesado en curso? Si lo hay, avisa al usuario (para comprobarlo antes de guardar nada)."""
        other = self.manager.current_job
        if other:
            self._tell_busy(other)
        return bool(other)

    def begin_job(self, name: str) -> bool:
        """Reserva la GPU para `name`; si otro trabajo la tiene, lo explica y devuelve False."""
        other = self.manager.begin_job(name)
        if other:
            self._tell_busy(other)
        return other is None

    def end_job(self, name: str) -> None:
        self.manager.end_job(name)

    @staticmethod
    def _tell_busy(other: str) -> None:
        messagebox.showinfo("La GPU está ocupada", f"Ahora mismo está en curso: «{other}».\n\nCon 8 GB de VRAM solo cabe un trabajo "
                            "pesado a la vez (se descargarían los modelos el uno al otro). Espera a que termine o cancélalo.")

    def _tick_status(self):
        free = ""
        try:
            info = hardware_detector._nvml()
            if info is not None:
                free = f"VRAM libre {info.vram_free_gb:.1f} de {info.vram_total_gb:.1f} GB"
        except Exception as exc:  # noqa: BLE001  (la barra de estado es informativa: nunca debe romper la ventana)
            log.debug("No se pudo leer la VRAM: %s", exc)
        mem = f"En memoria: {self._state_text}" if self._state_text != "libre" else "Sin modelo cargado"
        job = f"Trabajando: {self.manager.current_job}" if self.manager.current_job else ""
        self.status_lbl.configure(text="  ·  ".join(x for x in (job, mem, free) if x))
        self.after(2500, self._tick_status)

    # --------------------------------------------------------------- páginas
    def page(self, key: str):
        """La página `key`, creándola si hace falta."""
        if key not in self.pages:
            _, mod, cls = PAGES[key]
            try:
                klass = getattr(importlib.import_module(f"{__package__}.{mod}"), cls)
                self.pages[key] = klass(self.content, self)
            except Exception as exc:
                log.exception("No se pudo abrir la página %s", key)
                messagebox.showerror("No se pudo abrir", f"{PAGES[key][0]}\n\n{type(exc).__name__}: {exc}")
                return None
        return self.pages[key]

    def show(self, key: str):
        page = self.page(key)
        if page is None:
            return
        for p in self.pages.values():
            p.grid_forget()
        page.grid(row=0, column=0, sticky="nsew")
        self.active = key
        for k, b in self.nav.items():
            b.configure(fg_color=BORDER if k == key else "transparent", text_color=ACCENT if k == key else TEXT,
                        font=font(14, "bold" if k == key else "normal"))
        if hasattr(page, "on_show"):
            page.on_show()

    def send_to_editor(self, path: str):
        """Desde «Generar imagen»: abre la imagen en «Editar imagen»."""
        page = self.page("edit")
        if page is not None:
            self.show("edit")
            page.open_path(path)

    # ------------------------------------------------------------------ tema
    def _toggle_dark(self):
        self.dark = bool(self.dark_switch.get())
        ctk.set_appearance_mode("dark" if self.dark else "light")
        theme.save_dark(self.dark)
        for p in self.pages.values():
            if hasattr(p, "on_theme"):
                p.on_theme()

    # --------------------------------------------------------------- eventos
    def _shortcut(self, name: str):
        page = self.pages.get(self.active or "")
        if page is not None and hasattr(page, "shortcut"):
            page.shortcut(name)

    def _setup_dnd(self):
        if not HAS_DND:
            return
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as exc:  # noqa: BLE001
            log.warning("Drag & drop no disponible: %s", exc)

    def _on_drop(self, event):
        paths = [str(p) for p in self.tk.splitlist(event.data)]
        images = [p for p in paths if p.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))]
        if images and self.active == "edit":
            self.pages["edit"].open_path(images[0])
        elif any(p.lower().endswith(".pdf") for p in paths):
            self.show("pdf")
            self.pages["pdf"].add_paths(paths)

    def _on_close(self):
        busy = [PAGES[k][0] for k, p in self.pages.items() if hasattr(p, "busy") and p.busy()]
        if busy and not messagebox.askyesno("Salir", f"Hay trabajo en curso ({', '.join(busy)}). ¿Cancelar y salir?"):
            return
        for p in self.pages.values():
            if hasattr(p, "shutdown"):
                p.shutdown()
        close_clients()
        self.destroy()


def run():
    StudioApp().mainloop()
