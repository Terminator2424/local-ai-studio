"""Página «Generar imagen»: Stable Diffusion local (SD 1.5 y SDXL en fp16) con descarga confirmada y verificada.

La generación y las descargas corren en hilos aparte; la interfaz solo recibe mensajes por una cola.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..core.errors import Cancelled
from ..images import clipboard
from ..images import models as mdl
from ..images.generate import GenerationError, GenParams, ImageGenerator
from .theme import (ACCENT, BORDER, MUTED, OK, SURFACE, TEXT, WARN, font, ghost_button, label, option_menu,
                    primary_button, slider)

log = logging.getLogger(__name__)
PRESETS = {"512 × 512": (512, 512), "512 × 768": (512, 768), "768 × 512": (768, 512), "768 × 768": (768, 768),
           "1024 × 1024": (1024, 1024), "1024 × 768": (1024, 768), "768 × 1024": (768, 1024)}


class GenPage(ctk.CTkFrame):
    title = "Generar imagen"

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.gen: ImageGenerator | None = None
        self.results: list = []
        self.current = 0
        self.working = False
        self._thumb_refs: list = []

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_controls()
        self._build_preview()
        self._refresh_model_status()
        self.after(100, self._poll)

    def busy(self) -> bool:
        return self.working

    def shutdown(self):
        self.cancel.set()

    def shortcut(self, name: str):
        if name == "run":
            self._generate()

    def on_theme(self):
        pass

    # ------------------------------------------------------------ controles
    def _build_controls(self):
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        wrap.grid_rowconfigure(0, weight=1)
        left = ctk.CTkScrollableFrame(wrap, width=330, fg_color="transparent", scrollbar_button_color=BORDER)
        left.grid(row=0, column=0, sticky="nsew")
        footer = ctk.CTkFrame(wrap, fg_color="transparent")        # el botón principal siempre a la vista
        footer.grid(row=1, column=0, sticky="ew", padx=(0, 14), pady=(6, 0))

        label(left, "Modelo", 13, "bold").pack(anchor="w", padx=6, pady=(0, 4))
        self.model_var = ctk.StringVar(value="sd15")
        self.model_menu = option_menu(left, [m.title for m in mdl.CATALOG.values()], None, self._on_model)
        self.model_menu.set(mdl.CATALOG["sd15"].title)
        self.model_menu.pack(fill="x", padx=6)
        self.model_status = label(left, "", 12, muted=True, wraplength=300, justify="left")
        self.model_status.pack(anchor="w", padx=8, pady=(6, 0))
        self.dl_btn = ghost_button(left, "Descargar modelo…", self._download, height=30)
        self.dl_progress = ctk.CTkLabel(left, text="", font=font(11), text_color=MUTED, wraplength=300, justify="left")

        label(left, "Prompt", 13, "bold").pack(anchor="w", padx=6, pady=(16, 4))
        self.prompt = ctk.CTkTextbox(left, height=92, wrap="word", fg_color=SURFACE, text_color=TEXT, border_width=1,
                                     border_color=BORDER, font=font(13))
        self.prompt.pack(fill="x", padx=6)
        label(left, "Negative prompt", 12, muted=True).pack(anchor="w", padx=6, pady=(10, 4))
        self.negative = ctk.CTkTextbox(left, height=52, wrap="word", fg_color=SURFACE, text_color=TEXT, border_width=1,
                                       border_color=BORDER, font=font(12))
        self.negative.pack(fill="x", padx=6)
        self.negative.insert("1.0", "borroso, baja calidad, deformado, texto, marca de agua")

        self.steps_var = ctk.IntVar(value=25)
        self.cfg_var = ctk.DoubleVar(value=7.0)
        self.w_var = ctk.IntVar(value=512)
        self.h_var = ctk.IntVar(value=512)
        self.steps_lbl = self._slider_row(left, "Pasos", self.steps_var, 20, 50, 30)
        self.cfg_lbl = self._slider_row(left, "CFG", self.cfg_var, 1, 15, 28)
        label(left, "Resolución", 12).pack(anchor="w", padx=6, pady=(10, 2))
        self.preset = option_menu(left, list(PRESETS), None, self._on_preset)
        self.preset.set("512 × 512")
        self.preset.pack(fill="x", padx=6)
        self.res_lbl = label(left, "", 11, muted=True)
        self.res_lbl.pack(anchor="w", padx=8, pady=(2, 0))

        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(12, 0))
        label(row, "Semilla", 12).pack(side="left")
        self.seed_var = ctk.StringVar(value="")
        self.seed_entry = ctk.CTkEntry(row, textvariable=self.seed_var, width=110, height=28, fg_color=SURFACE, border_color=BORDER,
                                       text_color=TEXT, placeholder_text="aleatoria")
        self.seed_entry.pack(side="left", padx=8)
        self.random_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(row, text="aleatoria", variable=self.random_var, width=20, checkbox_width=18, checkbox_height=18,
                        fg_color=ACCENT, hover_color=ACCENT, text_color=TEXT, font=font(12)).pack(side="left")

        row2 = ctk.CTkFrame(left, fg_color="transparent")
        row2.pack(fill="x", padx=6, pady=(10, 0))
        label(row2, "Lote", 12).pack(side="left")
        self.batch = ctk.CTkSegmentedButton(row2, values=["1", "2", "3", "4"], selected_color=ACCENT, selected_hover_color=ACCENT,
                                            unselected_color=SURFACE, text_color=TEXT, font=font(12), width=150)
        self.batch.set("1")
        self.batch.pack(side="left", padx=10)

        self.gen_btn = primary_button(footer, "Generar", self._generate, height=44)
        self.gen_btn.pack(fill="x", padx=6, pady=(0, 2))
        self.cancel_btn = ctk.CTkButton(footer, text="Cancelar", height=28, fg_color="transparent", hover_color=SURFACE,
                                        text_color=MUTED, font=font(12), command=self.cancel.set)
        self.progress = ctk.CTkProgressBar(footer, height=5, progress_color=ACCENT, fg_color=BORDER)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=6, pady=(6, 0))
        self.status = label(footer, "", 12, muted=True, wraplength=300, justify="left")
        self.status.pack(anchor="w", padx=8, pady=(4, 4))
        self._upd_labels()

    def _slider_row(self, parent, text, var, lo, hi, steps):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(10, 0))
        label(row, text, 12).pack(side="left")
        val = label(row, "", 12, muted=True)
        val.pack(side="right")
        s = slider(parent, from_=lo, to=hi, number_of_steps=steps, variable=var, command=lambda v: self._upd_labels())
        s.pack(fill="x", padx=6)
        return val

    def _upd_labels(self):
        self.steps_lbl.configure(text=str(int(self.steps_var.get())))
        self.cfg_lbl.configure(text=f"{self.cfg_var.get():.1f}")
        self.res_lbl.configure(text=f"{self.w_var.get()} × {self.h_var.get()} px  ·  nativa del modelo: {self.model.native}")

    @property
    def model(self) -> mdl.ImageModel:
        title = self.model_menu.get()
        return next(m for m in mdl.CATALOG.values() if m.title == title)

    def _on_preset(self, value):
        self.w_var.set(PRESETS[value][0])
        self.h_var.set(PRESETS[value][1])
        self._upd_labels()

    def _on_model(self, _):
        n = self.model.native
        self.preset.set(f"{n} × {n}")
        self._on_preset(f"{n} × {n}")
        self.steps_var.set(25 if n == 512 else 30)
        self._refresh_model_status()

    def _refresh_model_status(self):
        m = self.model
        self._upd_labels()
        if mdl.is_installed(m):
            self.model_status.configure(text=f"Instalado.  Licencia {m.license}.  {m.note}", text_color=OK)
            self.dl_btn.pack_forget()
        else:
            self.model_status.configure(text=f"No descargado (≈ {m.size_mb / 1000:.1f} GB).  {m.note}", text_color=WARN)
            self.dl_btn.pack(fill="x", padx=6, pady=(6, 0), after=self.model_status)

    # ----------------------------------------------------------- descargar
    def _download(self):
        m = self.model
        if self.working:
            return
        ok = messagebox.askyesno(
            "Descargar modelo de imágenes",
            f"{m.title}\n\nOrigen: huggingface.co/{m.repo}\nRevisión fija: {m.revision[:10]}…\nLicencia: {m.license}\n"
            f"Tamaño: ≈ {m.size_mb / 1000:.1f} GB (solo archivos .safetensors y de configuración)\n\n"
            "Se comprobará el SHA-256 de cada peso y, si no coincide, se borrará.\n"
            "Es la única conexión a Internet de esta función.\n\n¿Descargar ahora?")
        if not ok:
            return
        self.working = True
        self.gen_btn.configure(state="disabled")
        self.dl_btn.configure(state="disabled")
        self.dl_progress.pack(anchor="w", padx=8, pady=(4, 0), after=self.dl_btn)
        threading.Thread(target=self._download_worker, args=(m,), daemon=True).start()

    def _download_worker(self, m):
        try:
            mdl.download(m, lambda s: self.q.put(("dl", s)))
            self.q.put(("dl_done",))
        except Exception as exc:
            log.exception("Descarga del modelo")
            self.q.put(("dl_error", f"{type(exc).__name__}: {exc}"))

    # ------------------------------------------------------------ generar
    def _read_params(self) -> GenParams | None:
        seed = None
        if not self.random_var.get():
            try:
                seed = int(self.seed_var.get())
            except ValueError:
                messagebox.showerror("Semilla no válida", "Escribe un número entero o marca «aleatoria».")
                return None
        return GenParams(prompt=self.prompt.get("1.0", "end").strip(), negative=self.negative.get("1.0", "end").strip(),
                         steps=int(self.steps_var.get()), cfg=round(float(self.cfg_var.get()), 1),
                         width=int(self.w_var.get()), height=int(self.h_var.get()), seed=seed, batch=int(self.batch.get()))

    def _generate(self):
        if self.working:
            return
        m = self.model
        if not mdl.is_installed(m):
            messagebox.showinfo("Falta el modelo", "Primero descarga el modelo (botón «Descargar modelo…»).")
            return
        params = self._read_params()
        if params is None:
            return
        try:
            params.validate(m)
        except GenerationError as exc:
            messagebox.showwarning("Revisa los parámetros", str(exc))
            return
        if self.gen is None:
            self.gen = ImageGenerator(self.app.manager)
        self.cancel.clear()
        self.working = True
        self.gen_btn.configure(state="disabled", text="Generando…")
        self.cancel_btn.pack(fill="x", padx=6, after=self.gen_btn)
        self.progress.set(0)
        self.status.configure(text="Preparando (descargando el chat de la VRAM y cargando el modelo)…")
        threading.Thread(target=self._gen_worker, args=(m.key, params), daemon=True).start()

    def _gen_worker(self, key: str, params: GenParams):
        try:
            if self.gen is None:
                raise RuntimeError("El generador de imágenes no está inicializado.")
            res = self.gen.generate(key, params, self.cancel, lambda f, s: self.q.put(("progress", f, s)))
            self.q.put(("done", res))
        except Cancelled:
            self.q.put(("cancelled",))
        except GenerationError as exc:
            self.q.put(("error", str(exc)))
        except Exception as exc:
            log.exception("Generación de imagen")
            self.q.put(("error", f"{type(exc).__name__}: {exc}"))

    # ----------------------------------------------------------- vista previa
    def _build_preview(self):
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)
        card = ctk.CTkFrame(right, fg_color=SURFACE, corner_radius=12, border_width=1, border_color=BORDER)
        card.grid(row=0, column=0, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)
        self.canvas = ctk.CTkLabel(card, text="La imagen aparecerá aquí.", text_color=MUTED, font=font(14))
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        card.bind("<Configure>", lambda e: self._show(self.current))
        self.thumbs = ctk.CTkFrame(right, fg_color="transparent")
        self.thumbs.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        bar = ctk.CTkFrame(right, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.info = label(bar, "", 12, muted=True)
        self.info.pack(side="left")
        for text, cmd in (("Abrir carpeta", self._open_folder), ("Editar", self._to_editor), ("Copiar", self._copy),
                          ("Guardar como…", self._save_as)):
            ghost_button(bar, text, cmd, width=104, height=32).pack(side="right", padx=(6, 0))

    def _show(self, idx: int):
        if not self.results:
            return
        idx = max(0, min(idx, len(self.results) - 1))
        self.current = idx
        res = self.results[idx]
        w = max(200, self.canvas.winfo_width() - 24)
        h = max(200, self.canvas.winfo_height() - 24)
        iw, ih = res.image.size
        sc = min(w / iw, h / ih, 1.5)
        img = ctk.CTkImage(light_image=res.image, dark_image=res.image, size=(max(1, int(iw * sc)), max(1, int(ih * sc))))
        self.canvas.configure(image=img, text="")
        self._img_ref = img
        self.info.configure(text=f"semilla {res.seed}  ·  {iw}×{ih}  ·  {res.path.name}")

    def _fill_thumbs(self):
        for w in self.thumbs.winfo_children():
            w.destroy()
        self._thumb_refs.clear()
        if len(self.results) < 2:
            return
        for i, r in enumerate(self.results):
            t = r.image.copy()
            t.thumbnail((84, 84))
            ci = ctk.CTkImage(light_image=t, dark_image=t, size=t.size)
            self._thumb_refs.append(ci)
            ctk.CTkButton(self.thumbs, text="", image=ci, width=90, height=90, fg_color="transparent", hover_color=BORDER,
                          border_width=1, border_color=BORDER, command=lambda i=i: self._show(i)).pack(side="left", padx=(0, 6))

    # ------------------------------------------------------------- acciones
    def _cur(self):
        if not self.results:
            messagebox.showinfo("Sin imagen", "Primero genera una imagen.")
            return None
        return self.results[self.current]

    def _save_as(self):
        r = self._cur()
        if r is None:
            return
        path = filedialog.asksaveasfilename(title="Guardar imagen", initialfile=r.path.name, defaultextension=".png",
                                            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg"), ("WebP", "*.webp")])
        if path:
            from ..images import edit
            edit.save(r.image, path)
            self.status.configure(text=f"Guardada en {path}")

    def _copy(self):
        r = self._cur()
        if r is None:
            return
        ok = clipboard.copy_image(r.image)
        self.status.configure(text="Imagen copiada al portapapeles." if ok else "No se pudo copiar la imagen.")

    def _open_folder(self):
        mdl.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(mdl.OUTPUTS_DIR))     # noqa: S606 (carpeta local propia)

    def _to_editor(self):
        r = self._cur()
        if r is not None:
            self.app.send_to_editor(str(r.path))

    # ----------------------------------------------------------------- cola
    def _poll(self):
        try:
            for _ in range(60):
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _end_work(self):
        self.working = False
        self.gen_btn.configure(state="normal", text="Generar")
        self.cancel_btn.pack_forget()
        self.dl_btn.configure(state="normal")

    def _handle(self, msg):
        kind = msg[0]
        if kind == "progress":
            self.progress.set(max(0.0, min(1.0, msg[1])))
            self.status.configure(text=msg[2])
        elif kind == "done":
            self._end_work()
            self.results = msg[1]
            self.progress.set(1.0)
            self.status.configure(text=f"Listo: {len(self.results)} imagen(es) guardada(s) en outputs/")
            self._fill_thumbs()
            self._show(0)
        elif kind == "cancelled":
            self._end_work()
            self.status.configure(text="Cancelado.")
        elif kind == "error":
            self._end_work()
            self.status.configure(text="Error: " + msg[1])
            messagebox.showerror("No se pudo generar la imagen", msg[1])
        elif kind == "dl":
            self.dl_progress.configure(text=msg[1])
        elif kind == "dl_done":
            self._end_work()
            self.dl_progress.pack_forget()
            self._refresh_model_status()
            messagebox.showinfo("Modelo descargado", "Descarga completada y hashes verificados.")
        elif kind == "dl_error":
            self._end_work()
            self.dl_progress.pack_forget()
            self._refresh_model_status()
            messagebox.showerror("Descarga fallida", msg[1])
