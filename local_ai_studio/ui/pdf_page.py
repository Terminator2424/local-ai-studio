"""Página «Resumir PDF» de Local AI Studio (CustomTkinter): simple, tema compartido (blanco/oscuro, acento rojo).

Lo esencial a la vista (elegir PDF, nivel, Resumir, resultado, Guardar/Copiar); el resto, plegado.
El procesamiento corre en un hilo aparte y se comunica con la GUI mediante una cola.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import ClassVar

import customtkinter as ctk

from ..core.hardware_detector import describe as describe_hardware
from ..pdf.summarizer import UI, Summarizer
from ..core.config import load_config
from ..core.errors import Cancelled, PDFError
from ..core.logsetup import setup_logging
from ..core.paths import CACHE_DIR, SUMMARIES_DIR
from ..core.pdf_export import export_pdf
from ..core.textutil import fmt_time, safe_name
from .theme import (ACCENT, ACCENT_H, BORDER, ERR, FONT, LOG_COLORS, MUTED, OK, SURFACE, TEXT, WARN)

try:  # visor HTML con fórmulas dibujadas (matplotlib); si falta, se usa un visor de texto
    from tkinterweb import HtmlFrame
    HAS_WEB = True
except Exception:
    HAS_WEB = False

log = logging.getLogger(__name__)

LEVELS = ["Texto limpio", "Compacto IA", "Normal", "Detallado"]
LEVEL_HINTS = {
    "Texto limpio": "Todo el contenido del PDF, ordenado y sin basura (código, tablas y fórmulas incluidos). "
                    "Sin inventar nada; el más fiel.",
    "Compacto IA": "Telegráfico: lo que una IA necesita para entender el documento, con el mínimo de tokens.",
    "Normal": "Equilibrado: más contexto y explicaciones breves.",
    "Detallado": "Lo más completo; puede ser tan largo como el PDF.",
}
N_STEPS = 7
STEP_ORDER = {"extract": 1, "vision": 2, "figures": 3, "map": 4, "verify": 5, "reduce": 6, "refine": 7}


class QueueUI(UI):
    """Los callbacks del hilo de trabajo solo ponen mensajes en una cola (thread-safe)."""
    def __init__(self, q: queue.Queue):
        self.q = q

    def progress(self, frac, msg):
        self.q.put(("progress", frac, msg))

    def log(self, msg):
        self.q.put(("log", msg))

    def live(self, text):
        pass

    def phase(self, key):
        self.q.put(("phase", key))

    def stats(self, **kw):
        self.q.put(("stats", kw))


class _Prefixed(UI):
    """Antepone «[k/N]» al estado de progreso cuando hay varios PDFs."""
    def __init__(self, inner: UI, n: int, total: int):
        self.inner, self.n, self.total = inner, n, total

    def progress(self, frac, msg):
        self.inner.progress(frac, f"[{self.n}/{self.total}] {msg}" if self.total > 1 else msg)

    def log(self, msg):
        self.inner.log(msg)

    def live(self, text):
        self.inner.live(text)

    def phase(self, key):
        self.inner.phase(key)

    def stats(self, **kw):
        self.inner.stats(**kw)


class QueueLogHandler(logging.Handler):
    """Muestra en el registro las advertencias/errores del logger."""
    def __init__(self, q):
        super().__init__(level=logging.WARNING)
        self.q = q

    def emit(self, record):
        self.q.put(("log", f"⚠ {record.getMessage()}"))


# ====================================================================== App
class PdfPage(ctk.CTkFrame):
    title = "Resumir PDF"

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.cfg = app.cfg
        self.q: queue.Queue = queue.Queue()
        setup_logging(QueueLogHandler(self.q))
        self.cancel = threading.Event()
        self.files: list[dict] = []          # {path, status, result}
        self.current: int | None = None
        self.current_md = ""
        self.t0, self.frac, self.step = 0.0, 0.0, 0
        self.running = False
        self.opts_open = False
        self.log_open = False
        self._html_token = 0

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_left()
        self._build_main()
        self._apply_log_colors()
        self.after(100, self._poll)
        self._show_empty()
        self.after(900, self._restore_queue)

    # ----------------------------------------------------------- tema
    @property
    def dark(self) -> bool:
        return self.app.dark

    def on_theme(self):
        """La ventana principal avisa al cambiar entre modo claro y oscuro."""
        self._apply_log_colors()
        if self.current_md:
            self._render(self.current_md)
        else:
            self._show_empty()

    def _apply_log_colors(self):
        t = self.log_box._textbox
        for tag, color in LOG_COLORS["dark" if self.dark else "light"].items():
            t.tag_configure(tag, foreground=color)

    # ---------------------------------------------------------- columna izquierda
    def _build_left(self):
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        wrap.grid_rowconfigure(0, weight=1)
        left = ctk.CTkScrollableFrame(wrap, width=290, fg_color="transparent", scrollbar_button_color=BORDER)
        left.grid(row=0, column=0, sticky="nsew")
        footer = ctk.CTkFrame(wrap, fg_color="transparent")        # el botón principal siempre a la vista
        footer.grid(row=1, column=0, sticky="ew", padx=(0, 14), pady=(6, 0))

        self._label(left, "Documento")
        drop = ctk.CTkFrame(left, fg_color=SURFACE, corner_radius=10, border_width=1, border_color=BORDER)
        drop.pack(fill="x", padx=6)
        self.drop_label = ctk.CTkLabel(drop, text="Arrastra un PDF aquí" if self.app.has_dnd else "Añade un PDF",
                                       font=ctk.CTkFont(FONT, 13), text_color=MUTED)
        self.drop_label.pack(pady=(16, 6))
        ctk.CTkButton(drop, text="Elegir PDF…", height=32, width=140, corner_radius=8, fg_color="transparent",
                      border_width=1, border_color=ACCENT, text_color=ACCENT, hover_color=SURFACE,
                      font=ctk.CTkFont(FONT, 13), command=self._add_files).pack(pady=(0, 6))
        ctk.CTkButton(drop, text="Añadir carpeta…", height=28, width=140, corner_radius=8, fg_color="transparent",
                      text_color=MUTED, hover_color=SURFACE, font=ctk.CTkFont(FONT, 12),
                      command=self._add_folder).pack(pady=(0, 14))

        self.file_list = ctk.CTkFrame(left, fg_color="transparent", height=0)     # sin height=0 un marco vacío mide 200 px
        self.file_list.pack(fill="x", padx=6, pady=(8, 0))

        self._label(left, "Nivel")
        default_detail = self.cfg["gui"]["default_detail"]
        self.detail_var = ctk.StringVar(value=default_detail if default_detail in LEVELS else LEVELS[0])
        ctk.CTkOptionMenu(left, values=LEVELS, variable=self.detail_var, command=self._on_detail, height=34,
                          fg_color=SURFACE, button_color=ACCENT, button_hover_color=ACCENT_H, text_color=TEXT,
                          dropdown_fg_color=SURFACE, dropdown_text_color=TEXT, dropdown_hover_color=BORDER,
                          font=ctk.CTkFont(FONT, 13, "bold")).pack(fill="x", padx=6)
        self.detail_hint = ctk.CTkLabel(left, text="", text_color=MUTED, font=ctk.CTkFont(FONT, 12), wraplength=260, justify="left")
        self.detail_hint.pack(anchor="w", padx=8, pady=(6, 0))

        self.run_btn = ctk.CTkButton(footer, text="Resumir", height=44, corner_radius=10, fg_color=ACCENT, hover_color=ACCENT_H,
                                     text_color="#ffffff", font=ctk.CTkFont(FONT, 15, "bold"), command=self._start)
        self.run_btn.pack(fill="x", padx=6, pady=(0, 2))
        self.cancel_btn = ctk.CTkButton(footer, text="Cancelar", height=28, fg_color="transparent", hover_color=SURFACE,
                                        text_color=MUTED, font=ctk.CTkFont(FONT, 12), command=self._cancel)

        # --- opciones (plegadas)
        self.opts_btn = ctk.CTkButton(left, text="Opciones  ▸", anchor="w", height=28, fg_color="transparent", hover_color=SURFACE,
                                      text_color=MUTED, font=ctk.CTkFont(FONT, 12), command=self._toggle_opts)
        self.opts_btn.pack(fill="x", padx=2, pady=(18, 0))
        self.opts = ctk.CTkFrame(left, fg_color="transparent")
        self.lang_var = ctk.StringVar(value=self.cfg["gui"]["default_language"])
        ctk.CTkOptionMenu(self.opts, values=["Auto-detectar", "Español", "English"], variable=self.lang_var, height=30,
                          fg_color=SURFACE, button_color=BORDER, button_hover_color=ACCENT, text_color=TEXT,
                          dropdown_fg_color=SURFACE, dropdown_text_color=TEXT,
                          font=ctk.CTkFont(FONT, 12)).pack(fill="x", padx=6, pady=(4, 8))
        self.math_var = ctk.BooleanVar(value=True)
        self.double_var = ctk.BooleanVar(value=bool(self.cfg["extraction"].get("double_vision", True)))
        self.verify_var = ctk.BooleanVar(value=True)
        self.fig_var = ctk.BooleanVar(value=bool(self.cfg["gui"]["describe_figures"]))
        self.cache_var = ctk.BooleanVar(value=bool(self.cfg["gui"]["use_cache"]))
        for txt, var in (("Leer fórmulas con visión", self.math_var), ("Leerlas dos veces (más fiable, más lento)", self.double_var),
                         ("Verificar contra el PDF", self.verify_var),
                         ("Describir gráficos", self.fig_var), ("Usar caché", self.cache_var)):
            ctk.CTkSwitch(self.opts, text=txt, variable=var, progress_color=ACCENT, font=ctk.CTkFont(FONT, 12),
                          text_color=TEXT).pack(anchor="w", padx=8, pady=3)
        self.chunk_var = ctk.StringVar(value=str(self.cfg["chunking"]["chunk_size"]))
        self.overlap_var = ctk.StringVar(value=str(self.cfg["chunking"]["chunk_overlap"]))
        self.iter_var = ctk.StringVar(value="1")
        row = ctk.CTkFrame(self.opts, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(8, 0))
        for i, (txt, var) in enumerate((("Chunk", self.chunk_var), ("Overlap", self.overlap_var), ("Iter.", self.iter_var))):
            ctk.CTkLabel(row, text=txt, text_color=MUTED, font=ctk.CTkFont(FONT, 11)).grid(row=0, column=i, padx=4)
            ctk.CTkEntry(row, textvariable=var, width=62, height=28, justify="center", fg_color=SURFACE,
                         border_color=BORDER, text_color=TEXT).grid(row=1, column=i, padx=4)
        self.diag_lbl = ctk.CTkLabel(self.opts, text=describe_hardware(self.cfg), justify="left", anchor="w", wraplength=250,
                                     text_color=MUTED, font=ctk.CTkFont(FONT, 11))
        self.diag_lbl.pack(anchor="w", padx=8, pady=(12, 4))
        self._on_detail(self.detail_var.get())

    @staticmethod
    def _label(parent, text):
        ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(FONT, 13, "bold"), text_color=TEXT).pack(anchor="w", padx=8, pady=(16, 6))

    def _toggle_opts(self):
        self.opts_open = not self.opts_open
        if self.opts_open:
            self.opts.pack(fill="x", after=self.opts_btn)
            self.opts_btn.configure(text="Opciones  ▾")
        else:
            self.opts.pack_forget()
            self.opts_btn.configure(text="Opciones  ▸")

    # --------------------------------------------------------- zona principal
    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=(14, 0), pady=(0, 0))
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(main, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        self.status_lbl = ctk.CTkLabel(head, text="Listo para empezar", font=ctk.CTkFont(FONT, 14, "bold"), anchor="w", text_color=TEXT)
        self.status_lbl.grid(row=0, column=0, sticky="ew")
        self.time_lbl = ctk.CTkLabel(head, text="", text_color=MUTED, font=ctk.CTkFont(FONT, 12))
        self.time_lbl.grid(row=0, column=1)
        self.progress = ctk.CTkProgressBar(head, height=5, corner_radius=3, progress_color=ACCENT, fg_color=BORDER)
        self.progress.set(0)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        card = ctk.CTkFrame(main, fg_color=SURFACE, corner_radius=12, border_width=1, border_color=BORDER)
        card.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)
        self.web = None
        if HAS_WEB:
            try:
                self.web = HtmlFrame(card, messages_enabled=False)
                self.web.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
            except Exception as exc:
                log.warning("tkinterweb no disponible, se usa el visor de texto: %s", exc)
                self.web = None
        self.view = ctk.CTkTextbox(card, wrap="word", fg_color="transparent", font=ctk.CTkFont(FONT, 14), text_color=TEXT,
                                   border_spacing=16)
        if self.web is None:
            self.view.grid(row=0, column=0, sticky="nsew")

        bar = ctk.CTkFrame(main, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        bar.grid_columnconfigure(0, weight=1)
        self.stats_lbl = ctk.CTkLabel(bar, text="", text_color=MUTED, font=ctk.CTkFont(FONT, 12), anchor="w", justify="left",
                                      wraplength=400)
        self.stats_lbl.grid(row=0, column=0, sticky="ew")
        # el texto de estadísticas ocupa el ancho que dejan los botones y, si no cabe, pasa a otra línea (antes se cortaba)
        bar.bind("<Configure>", lambda e: self.stats_lbl.configure(
            wraplength=max(200, int(e.width / ctk.ScalingTracker.get_window_scaling(self)) - 300)))
        self.copy_btn = ctk.CTkButton(bar, text="Copiar para IA", width=130, height=34, corner_radius=8, fg_color="transparent",
                                      border_width=1, border_color=BORDER, text_color=TEXT, hover_color=SURFACE,
                                      font=ctk.CTkFont(FONT, 13), command=self._copy)
        self.copy_btn.grid(row=0, column=1, padx=(8, 0))
        self.save_btn = ctk.CTkButton(bar, text="Guardar", width=110, height=34, corner_radius=8, fg_color=ACCENT,
                                      hover_color=ACCENT_H, text_color="#ffffff", font=ctk.CTkFont(FONT, 13, "bold"),
                                      command=self._save)
        self.save_btn.grid(row=0, column=2, padx=(8, 0))

        self.log_btn = ctk.CTkButton(main, text="Registro  ▸", anchor="w", height=26, fg_color="transparent", hover_color=SURFACE,
                                     text_color=MUTED, font=ctk.CTkFont(FONT, 12), command=self._toggle_log)
        self.log_btn.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.log_box = ctk.CTkTextbox(main, height=130, fg_color=SURFACE, corner_radius=8, text_color=TEXT, border_width=1,
                                      border_color=BORDER, font=ctk.CTkFont("Consolas", 11), state="disabled", wrap="word")

    def _toggle_log(self):
        self.log_open = not self.log_open
        if self.log_open:
            self.log_box.grid(row=4, column=0, sticky="ew", pady=(4, 0))
            self.log_btn.configure(text="Registro  ▾")
        else:
            self.log_box.grid_forget()
            self.log_btn.configure(text="Registro  ▸")

    # ================================================================ archivos
    def add_paths(self, paths):
        """Para el arrastrar y soltar de la ventana principal."""
        self._add_paths(paths)

    def shortcut(self, name: str):
        {"open": self._add_files, "save": self._save, "run": self._start}.get(name, lambda: None)()

    def busy(self) -> bool:
        return self.running

    def shutdown(self):
        self.cancel.set()

    def _add_folder(self):
        """Todos los PDFs de una carpeta (y subcarpetas, hasta 300) a la cola."""
        d = filedialog.askdirectory(title="Carpeta con PDFs")
        if not d:
            return
        pdfs = sorted(Path(d).rglob("*.pdf"))
        if not pdfs:
            messagebox.showinfo("Sin PDFs", "No hay PDFs en esa carpeta.")
            return
        if len(pdfs) > 300:
            messagebox.showinfo("Muchos PDFs", f"Hay {len(pdfs)} PDFs; se añaden los primeros 300.")
        self._add_paths([str(x) for x in pdfs[:300]])

    # ---- cola persistente: si la aplicación se cierra a medias, se puede reanudar
    QUEUE_FILE = CACHE_DIR / "queue.json"

    def _save_queue(self):
        pend = [f["path"] for f in self.files if f["status"] in ("pendiente", "procesando", "error", "cancelado")]
        try:
            if pend:
                self.QUEUE_FILE.write_text(json.dumps({"pending": pend}), encoding="utf-8")
            else:
                self.QUEUE_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    def _restore_queue(self):
        try:
            pend = [p for p in json.loads(self.QUEUE_FILE.read_text(encoding="utf-8")).get("pending", []) if os.path.isfile(p)]
        except (OSError, ValueError, AttributeError):
            return
        if pend and not self.files and messagebox.askyesno(
                "Reanudar", f"La última vez quedaron {len(pend)} PDF(s) sin terminar. ¿Los añado a la cola?\n"
                            "(Lo que ya se procesó se recupera de la caché al instante.)"):
            self._add_paths(pend)

    def _add_files(self):
        self._add_paths(filedialog.askopenfilenames(title="Selecciona PDFs", filetypes=[("PDF", "*.pdf")]))

    def _add_paths(self, paths):
        for p in paths:
            p = str(p)
            if not p.lower().endswith(".pdf"):
                self._log(f"Ignorado (no es PDF): {p}")
                continue
            if any(f["path"] == p for f in self.files):
                continue
            self.files.append({"path": p, "status": "pendiente", "result": None})
        self._refresh_list()
        if self.current is None and self.files:
            self._select(0)

    def _remove(self, i: int):
        if self.running:
            return
        del self.files[i]
        self.current = None
        self._refresh_list()
        self._show_empty()

    STATUS_TXT: ClassVar[dict[str, str]] = {"pendiente": "", "procesando": "en curso…", "listo": "listo", "caché": "listo",
                                            "error": "error", "cancelado": "cancelado"}
    STATUS_COLOR: ClassVar[dict[str, tuple[str, str]]] = {"listo": OK, "caché": OK, "error": ERR, "cancelado": WARN, "procesando": ACCENT,
                                              "pendiente": MUTED}

    def _refresh_list(self):
        for w in self.file_list.winfo_children():
            w.destroy()
        for i, f in enumerate(self.files):
            sel = i == self.current and len(self.files) > 1
            row = ctk.CTkFrame(self.file_list, fg_color=SURFACE if sel else "transparent", corner_radius=8)
            row.pack(fill="x", pady=1)
            name = Path(f["path"]).name
            lbl = ctk.CTkLabel(row, text=(name if len(name) < 34 else name[:31] + "…"), anchor="w", text_color=TEXT,
                               font=ctk.CTkFont(FONT, 12))
            lbl.pack(side="left", padx=8, pady=5)
            ctk.CTkButton(row, text="×", width=22, height=22, fg_color="transparent", hover_color=BORDER, text_color=MUTED,
                          command=lambda i=i: self._remove(i)).pack(side="right", padx=(0, 4))
            ctk.CTkLabel(row, text=self.STATUS_TXT.get(f["status"], ""), text_color=self.STATUS_COLOR.get(f["status"], MUTED),
                         font=ctk.CTkFont(FONT, 11)).pack(side="right", padx=4)
            for w in (row, lbl):
                w.bind("<Button-1>", lambda e, i=i: self._select(i))
        self._save_queue()

    def _select(self, i: int):
        self.current = i
        self._refresh_list()
        res = self.files[i]["result"]
        if res:
            self.current_md = res["markdown"]
            self._render(res["markdown"])
            self._set_stats(res)
        else:
            self.current_md = ""
            self._show_empty()

    # ============================================================ ejecución
    def _on_detail(self, value):
        lv = self.cfg["detail_levels"].get(value, {})
        self.iter_var.set(str(lv.get("refine_iterations", 1)))
        self.detail_hint.configure(text=LEVEL_HINTS.get(value, ""))

    def _read_settings(self) -> dict | None:
        try:
            cs, co, it = int(self.chunk_var.get()), int(self.overlap_var.get()), int(self.iter_var.get())
            if cs < 300 or co < 0 or co >= cs or it < 0 or it > 6:
                raise ValueError
        except ValueError:
            messagebox.showerror("Opciones no válidas", "Chunk ≥ 300, overlap menor que el chunk e iteraciones entre 0 y 6.")
            return None
        lang = {"Auto-detectar": "auto", "Español": "es", "English": "en"}[self.lang_var.get()]
        return {"detail": self.detail_var.get(), "language": lang, "chunk_size": cs, "chunk_overlap": co,
                "refine_iterations": it, "use_cache": self.cache_var.get(), "describe_figures": self.fig_var.get(),
                "math_vision": self.math_var.get(), "double_vision": self.double_var.get(), "verify": self.verify_var.get()}

    def _start(self):
        if self.running:
            return
        if not self.files:
            messagebox.showinfo("Falta un PDF", "Elige un PDF primero.")
            return
        todo = [i for i, f in enumerate(self.files) if f["status"] in ("pendiente", "error", "cancelado")] or list(range(len(self.files)))
        settings = self._read_settings()
        if not settings:
            return
        if not self.app.begin_job("Resumir PDF"):
            return
        try:   # la VRAM libre cambia con el uso del PC: se reevalúa el hardware en cada ejecución
            self.cfg = load_config()
            self.diag_lbl.configure(text=describe_hardware(self.cfg))
        except Exception as exc:
            log.warning("No se pudo releer la configuración: %s", exc)
        self.cancel.clear()
        self.running = True
        self.t0, self.frac, self.step = time.time(), 0.0, 0
        self.run_btn.configure(state="disabled", text="Resumiendo…")
        self.cancel_btn.pack(fill="x", padx=6, after=self.run_btn)
        for i in todo:
            self.files[i]["status"] = "pendiente"
        self._refresh_list()
        threading.Thread(target=self._worker, args=(todo, settings), daemon=True).start()
        self._tick()

    def _worker(self, todo: list[int], settings: dict):
        """Hilo de trabajo: nunca toca widgets, solo envía mensajes a la cola."""
        try:
            self._run_queue(todo, settings)
        finally:
            self.app.end_job("Resumir PDF")
            self.q.put(("all_done",))

    def _run_queue(self, todo: list[int], settings: dict):
        ui = QueueUI(self.q)
        try:      # con 8 GB no caben a la vez un modelo de Ollama y el de imágenes: se libera el de imágenes
            self.app.manager.use_llm(self.cfg["ollama"]["vision_model"])
        except Exception as exc:
            log.warning("No se pudo preparar la VRAM: %s", exc)
        for n, i in enumerate(todo, 1):
            if self.cancel.is_set():
                break
            path = self.files[i]["path"]
            self.q.put(("file_start", i, n, len(todo)))
            try:
                result = Summarizer(self.cfg, settings, _Prefixed(ui, n, len(todo)), self.cancel).process(path)
                self.q.put(("file_done", i, result))
            except Cancelled:
                self.q.put(("file_cancel", i))
                break
            except PDFError as exc:
                log.error("%s: %s", Path(path).name, exc)
                self.q.put(("file_error", i, str(exc)))
            except Exception as exc:
                log.exception("Error procesando %s", path)
                self.q.put(("file_error", i, f"{type(exc).__name__}: {exc}"))

    def _cancel(self):
        self.cancel.set()
        self.cancel_btn.configure(state="disabled")
        self._log("Cancelando… (se detiene al terminar el paso en curso)")

    def _tick(self):
        if not self.running:
            return
        el = time.time() - self.t0
        txt = fmt_time(el)
        if self.frac > 0.05:
            txt += f" · quedan ≈ {fmt_time(el * (1 - self.frac) / self.frac)}"
        self.time_lbl.configure(text=txt)
        self.after(1000, self._tick)

    # ============================================================== cola GUI
    def _poll(self):
        try:
            for _ in range(60):
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "progress":
            self.frac = max(0.0, min(1.0, msg[1]))
            self.progress.set(self.frac)
            step = f"Paso {self.step} de {N_STEPS} · " if self.step else ""
            self.status_lbl.configure(text=f"{step}{msg[2]}")
        elif kind == "log":
            self._log(msg[1])
        elif kind == "phase":
            self.step = STEP_ORDER.get(msg[1], self.step)
        elif kind == "stats":
            self._set_stats(msg[1])
        elif kind == "html":
            if msg[1] == self._html_token and self.web is not None:
                try:
                    self.web.load_html(msg[2])
                except Exception as exc:
                    log.warning("No se pudo actualizar el visor: %s", exc)
        elif kind == "file_start":
            _, i, n, total = msg
            self.files[i]["status"] = "procesando"
            self._refresh_list()
            self.frac, self.step = 0.0, 0
            self.progress.set(0)
            self.stats_lbl.configure(text="")
            self.status_lbl.configure(text=f"Archivo {n} de {total}: empezando…")
        elif kind == "file_done":
            _, i, result = msg
            self.files[i]["result"] = result
            self.files[i]["status"] = "caché" if result.get("from_cache") else "listo"
            self._refresh_list()
            self._select(i)
        elif kind == "file_error":
            _, i, err = msg
            self.files[i]["status"] = "error"
            self._refresh_list()
            self._log(f"✖ {Path(self.files[i]['path']).name}: {err}")
            messagebox.showerror("No se pudo resumir", f"{Path(self.files[i]['path']).name}\n\n{err}")
        elif kind == "file_cancel":
            self.files[msg[1]]["status"] = "cancelado"
            self._refresh_list()
        elif kind == "all_done":
            self.running = False
            self.run_btn.configure(state="normal", text="Resumir")
            self.cancel_btn.pack_forget()
            self.cancel_btn.configure(state="normal")
            cancelled = self.cancel.is_set()
            self.status_lbl.configure(text="Cancelado" if cancelled else "Listo")
            self.time_lbl.configure(text=fmt_time(time.time() - self.t0))
            if not cancelled:
                self.progress.set(1.0)

    def _set_stats(self, res: dict):
        """Una sola línea con lo importante: páginas, tokens, ahorro y calidad."""
        bits = []
        if res.get("pages"):
            bits.append(f"{res['pages']} págs.")
        tk_ = res.get("tokens")
        if tk_:
            bits.append(f"{tk_['summary']:,} tokens".replace(",", "."))
            bits.append(f"{'−' if tk_['saving'] >= 0 else '+'}{abs(tk_['saving']):.0f} % frente al PDF completo")
        fd = res.get("fidelity")
        if fd and fd.get("summary"):
            bits.append(fd["summary"])
        q = res.get("quality")
        if q and not fd:
            bits.append(f"cobertura de cifras {q['coverage']:.0f} %")
            if q["unsupported"]:
                bits.append(f"{len(q['unsupported'])} cifras sin respaldo")
        if bits:
            self.stats_lbl.configure(text="  ·  ".join(bits))

    # ================================================================ texto
    def _log(self, text: str):
        tag = None
        s = text.lstrip()
        if s.startswith(("✔", "↺")):
            tag = "ok"
        elif s.startswith("▶"):
            tag = "run"
        elif s.startswith(("⚠", "Auditoría")):
            tag = "warn"
        elif s.startswith("✖"):
            tag = "err"
        t = self.log_box._textbox
        t.configure(state="normal")
        t.insert("end", f"{time.strftime('%H:%M:%S')}  ", "time")
        t.insert("end", text + "\n", tag or ())
        t.see("end")
        t.configure(state="disabled")

    def _show_empty(self):
        self.current_md = ""
        if self.web is not None:
            fg, bg = ("#9aa3b2", "#171a21") if self.dark else ("#6b7280", "#f6f7f9")
            self.web.load_html(f"<html><body style='background:{bg};color:{fg};font-family:{FONT},sans-serif;text-align:center;"
                               "padding-top:140px;font-size:15px'>El resumen aparecerá aquí.</body></html>")
        else:
            t = self.view._textbox
            t.configure(state="normal")
            t.delete("1.0", "end")
            t.insert("end", "\n\nEl resumen aparecerá aquí.")
            t.configure(state="disabled")

    def _render(self, md: str):
        """Visor HTML: primero una versión rápida (fórmulas en Unicode) y luego la definitiva con las fórmulas dibujadas."""
        if self.web is None:
            self._render_text(md)
            return
        from ..core.mdview import md_to_html
        theme = "dark" if self.dark else "light"
        self._html_token += 1
        token = self._html_token

        def page(html, css):
            return f"<html><head><meta charset='utf-8'><style>{css}</style></head><body>{html}</body></html>"
        try:
            self.web.load_html(page(*md_to_html(md, theme, FONT, formulas=False, safe=True)))
        except Exception as exc:
            log.warning("Fallo del visor HTML: %s", exc)
            self._render_text(md)
            return

        def work():
            try:
                self.q.put(("html", token, page(*md_to_html(md, theme, FONT, formulas=True, safe=True))))
            except Exception as exc:
                log.warning("No se pudieron dibujar las fórmulas: %s", exc)
        threading.Thread(target=work, daemon=True).start()

    def _render_text(self, md: str):
        """Visor de reserva (sin tkinterweb): Markdown casi en bruto con fórmulas en Unicode."""
        from ..core.latex_text import latex_to_text
        t = self.view._textbox
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", latex_to_text(md))
        t.configure(state="disabled")
        self.view.grid(row=0, column=0, sticky="nsew")

    # ============================================================ guardar
    def _save(self):
        md = self.current_md
        if not md:
            messagebox.showinfo("Nada que guardar", "Primero genera un resumen.")
            return
        stem = safe_name(Path(self.files[self.current]["path"]).stem) + "_resumen" if self.current is not None else "resumen"
        path = filedialog.asksaveasfilename(title="Guardar resumen", initialdir=SUMMARIES_DIR, initialfile=stem + ".md",
                                            defaultextension=".md", filetypes=[("Markdown", "*.md")])
        if not path:
            return
        base = Path(path).with_suffix("")
        try:
            Path(f"{base}.md").write_text(md + "\n", encoding="utf-8")
            export_pdf(md, f"{base}.pdf")
            try:
                from ..core.docx_export import md_to_docx
                md_to_docx(md, f"{base}.docx")
            except ImportError:
                log.info("python-docx no está instalado: no se genera el .docx")
        except Exception as exc:
            log.exception("Error al guardar")
            messagebox.showerror("Error al guardar", str(exc))
            return
        self._log(f"✔ Guardado: {base}.md, {base}.pdf y {base}.docx")
        self.status_lbl.configure(text=f"Guardado en {base.parent.name}")

    def _copy(self):
        if not self.current_md:
            return
        self.clipboard_clear()
        self.clipboard_append(self.current_md)
        self.status_lbl.configure(text="Copiado: pégalo en tu asistente de IA")
