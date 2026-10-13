"""Página «Editar imagen»: recorte, giros, volteos, tamaño y filtros con sliders y vista previa en vivo (Pillow)."""
from __future__ import annotations

import logging
from typing import Any
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from ..images import clipboard, edit
from ..images.edit import EditParams
from .theme import ACCENT, BORDER, MUTED, SURFACE, TEXT, font, ghost_button, label, primary_button, slider

log = logging.getLogger(__name__)

# nombre, mínimo, máximo, valor neutro (los ajustes de filtro)
FILTERS = [("brightness", "Brillo", 0.2, 2.0, 1.0), ("contrast", "Contraste", 0.2, 2.0, 1.0),
           ("saturation", "Saturación", 0.0, 2.0, 1.0), ("blur", "Desenfoque", 0.0, 10.0, 0.0),
           ("sharpen", "Nitidez", 0.0, 2.0, 0.0), ("sepia", "Sepia", 0.0, 1.0, 0.0)]


class EditPage(ctk.CTkFrame):
    title = "Editar imagen"

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.img = None
        self.path: Path | None = None
        self.rot = 0
        self._after = None
        self._ref = None
        self.vars: dict[str, ctk.DoubleVar] = {}
        self.val_lbl: dict[str, ctk.CTkLabel] = {}
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_controls()
        self._build_preview()

    def busy(self) -> bool:
        return False

    def on_theme(self):
        pass

    def shortcut(self, name: str):
        if name == "open":
            self._open()
        elif name == "save":
            self._save()

    # ------------------------------------------------------------ controles
    def _build_controls(self):
        left = ctk.CTkScrollableFrame(self, width=320, fg_color="transparent", scrollbar_button_color=BORDER)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        primary_button(left, "Abrir imagen…", self._open, height=40).pack(fill="x", padx=6, pady=(0, 4))
        self.name_lbl = label(left, "Ninguna imagen abierta (o arrastra una aquí).", 12, muted=True, wraplength=290, justify="left")
        self.name_lbl.pack(anchor="w", padx=8, pady=(0, 8))

        label(left, "Giro y volteo", 13, "bold").pack(anchor="w", padx=6, pady=(8, 4))
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=6)
        ghost_button(row, "⟲ 90°", lambda: self._rotate(-90), width=70, height=30).pack(side="left")
        ghost_button(row, "⟳ 90°", lambda: self._rotate(90), width=70, height=30).pack(side="left", padx=6)
        ghost_button(row, "180°", lambda: self._rotate(180), width=60, height=30).pack(side="left")
        self.flip_h = ctk.BooleanVar(value=False)
        self.flip_v = ctk.BooleanVar(value=False)
        self.gray = ctk.BooleanVar(value=False)
        for text, var in (("Voltear horizontal", self.flip_h), ("Voltear vertical", self.flip_v), ("Blanco y negro", self.gray)):
            ctk.CTkSwitch(left, text=text, variable=var, command=self._changed, progress_color=ACCENT, font=font(12),
                          text_color=TEXT).pack(anchor="w", padx=10, pady=(6, 0))

        label(left, "Recorte (% que se quita)", 13, "bold").pack(anchor="w", padx=6, pady=(16, 2))
        for key, text in (("cl", "Izquierda"), ("ct", "Arriba"), ("cr", "Derecha"), ("cb", "Abajo")):
            self._slider(left, key, text, 0, 90, 0, fmt="{:.0f} %", steps=90)

        label(left, "Tamaño final (px)", 13, "bold").pack(anchor="w", padx=6, pady=(16, 4))
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=6)
        self.w_var, self.h_var = ctk.StringVar(), ctk.StringVar()
        self.lock = ctk.BooleanVar(value=True)
        for var, tag in ((self.w_var, "w"), (self.h_var, "h")):
            e = ctk.CTkEntry(row, textvariable=var, width=80, height=28, justify="center", fg_color=SURFACE,
                             border_color=BORDER, text_color=TEXT)
            e.pack(side="left", padx=(0, 6))
            e.bind("<KeyRelease>", lambda ev, t=tag: self._size_typed(t))
        ctk.CTkCheckBox(row, text="proporción", variable=self.lock, width=20, checkbox_width=18, checkbox_height=18,
                        fg_color=ACCENT, hover_color=ACCENT, text_color=TEXT, font=font(12)).pack(side="left")
        ghost_button(left, "Tamaño original", self._size_reset, height=28).pack(anchor="w", padx=6, pady=(6, 0))

        label(left, "Ajustes", 13, "bold").pack(anchor="w", padx=6, pady=(16, 2))
        for key, text, lo, hi, neutral in FILTERS:
            self._slider(left, key, text, lo, hi, neutral, fmt="{:.2f}", steps=100)

        ghost_button(left, "Restablecer todo", self._reset, height=32).pack(fill="x", padx=6, pady=(16, 4))
        primary_button(left, "Guardar como…", self._save, height=40).pack(fill="x", padx=6, pady=(4, 2))
        ghost_button(left, "Copiar al portapapeles", self._copy, height=30).pack(fill="x", padx=6, pady=(4, 12))

    def _slider(self, parent, key, text, lo, hi, neutral, fmt, steps):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(6, 0))
        label(row, text, 12).pack(side="left")
        val = label(row, fmt.format(neutral), 12, muted=True)
        val.pack(side="right")
        var = ctk.DoubleVar(value=neutral)
        self.vars[key], self.val_lbl[key] = var, val
        s = slider(parent, from_=lo, to=hi, number_of_steps=steps, variable=var,
                   command=lambda v, k=key, f=fmt: (self.val_lbl[k].configure(text=f.format(float(v))), self._changed()))
        s.pack(fill="x", padx=6)
        setattr(self, f"_neutral_{key}", (neutral, fmt))

    # ---------------------------------------------------------- vista previa
    def _build_preview(self):
        card = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=12, border_width=1, border_color=BORDER)
        card.grid(row=0, column=1, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)
        self.canvas = ctk.CTkLabel(card, text="Abre una imagen para empezar.", text_color=MUTED, font=font(14))
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        card.bind("<Configure>", lambda e: self._changed())
        self.info = label(card, "", 12, muted=True)
        self.info.grid(row=1, column=0, sticky="w", padx=14, pady=(0, 10))

    # --------------------------------------------------------------- estado
    def _crop(self) -> tuple[float, float, float, float]:
        left, t = self.vars["cl"].get() / 100, self.vars["ct"].get() / 100
        r, b = 1 - self.vars["cr"].get() / 100, 1 - self.vars["cb"].get() / 100
        if r - left < 0.05:
            r = min(1.0, left + 0.05)
        if b - t < 0.05:
            b = min(1.0, t + 0.05)
        return (left, t, r, b)

    def _base_size(self) -> tuple[int, int]:
        """Tamaño tras recorte y giro (antes del cambio de tamaño)."""
        left, t, r, b = self._crop()
        w, h = self.img.size
        w, h = max(1, round((r - left) * w)), max(1, round((b - t) * h))
        return (h, w) if self.rot % 180 else (w, h)

    def params(self, with_resize: bool = True) -> EditParams:
        filters: dict[str, Any] = {k: round(float(self.vars[k].get()), 3) for k, *_ in FILTERS}
        p = EditParams(crop=self._crop(), rotate=self.rot, flip_h=self.flip_h.get(), flip_v=self.flip_v.get(),
                       grayscale=self.gray.get(), **filters)
        if with_resize and self.img is not None:
            try:
                w, h = int(self.w_var.get()), int(self.h_var.get())
                if (w, h) != self._base_size() and w > 0 and h > 0:
                    p.resize = (w, h)
            except ValueError:
                pass
        return p

    def _changed(self):
        if self._after is not None:
            self.after_cancel(self._after)
        self._after = self.after(70, self._refresh)

    def _refresh(self):
        self._after = None
        if self.img is None:
            return
        try:
            self._sync_size_if_needed()
            pv = edit.preview(self.img, self.params(with_resize=False), max_side=max(300, min(self.canvas.winfo_width(), 1100)))
        except ValueError as exc:
            self.info.configure(text=str(exc))
            return
        w = max(200, self.canvas.winfo_width() - 24)
        h = max(200, self.canvas.winfo_height() - 24)
        sc = min(w / pv.width, h / pv.height, 1.0)
        ci = ctk.CTkImage(light_image=pv, dark_image=pv, size=(max(1, int(pv.width * sc)), max(1, int(pv.height * sc))))
        self.canvas.configure(image=ci, text="")
        self._ref = ci
        bw, bh = self._base_size()
        p = self.params()
        rw, rh = p.resize or (bw, bh)
        self.info.configure(text=f"Original {self.img.width}×{self.img.height}  →  resultado {rw}×{rh}")

    # ----------------------------------------------------------- tamaño
    def _sync_size_if_needed(self):
        """Si el usuario no ha tocado el tamaño, sigue al recorte y al giro."""
        if getattr(self, "_size_touched", False):
            return
        w, h = self._base_size()
        self.w_var.set(str(w))
        self.h_var.set(str(h))

    def _size_typed(self, which: str):
        self._size_touched = True
        if self.img is None or not self.lock.get():
            self._changed()
            return
        bw, bh = self._base_size()
        try:
            if which == "w":
                self.h_var.set(str(edit.fit_size((bw, bh), w=int(self.w_var.get()))[1]))
            else:
                self.w_var.set(str(edit.fit_size((bw, bh), h=int(self.h_var.get()))[0]))
        except ValueError:
            pass
        self._changed()

    def _size_reset(self):
        self._size_touched = False
        self._changed()

    # ------------------------------------------------------------- acciones
    def _rotate(self, deg: int):
        self.rot = (self.rot + deg) % 360
        self._size_touched = False
        self._changed()

    def _reset(self):
        self.rot = 0
        for v in (self.flip_h, self.flip_v, self.gray):
            v.set(False)
        for k, var in self.vars.items():
            neutral, fmt = getattr(self, f"_neutral_{k}")
            var.set(neutral)
            self.val_lbl[k].configure(text=fmt.format(neutral))
        self._size_touched = False
        self._changed()

    def _open(self):
        path = filedialog.askopenfilename(title="Abrir imagen", filetypes=[("Imágenes", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if path:
            self.open_path(path)

    def open_path(self, path: str):
        try:
            self.img = edit.load(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("No se pudo abrir", f"{type(exc).__name__}: {exc}")
            return
        self.path = Path(path)
        self.name_lbl.configure(text=f"{self.path.name}  ({self.img.width}×{self.img.height})")
        self._reset()

    def _result(self):
        if self.img is None:
            messagebox.showinfo("Sin imagen", "Abre una imagen primero.")
            return None
        try:
            return edit.apply(self.img, self.params())
        except ValueError as exc:
            messagebox.showwarning("Revisa los ajustes", str(exc))
            return None

    def _save(self):
        out = self._result()
        if out is None:
            return
        path = filedialog.asksaveasfilename(
            title="Guardar imagen", initialfile=(self.path.stem + "_editada") if self.path else "imagen", defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg"), ("WebP", "*.webp")])
        if path:
            saved = edit.save(out, path)
            self.info.configure(text=f"Guardada en {saved}")

    def _copy(self):
        out = self._result()
        if out is not None:
            self.info.configure(text="Copiada al portapapeles." if clipboard.copy_image(out) else "No se pudo copiar.")
