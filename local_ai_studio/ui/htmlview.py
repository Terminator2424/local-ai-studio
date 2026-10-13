"""Visor de Markdown + LaTeX reutilizable (tkinterweb) en MODO SEGURO: sin HTML crudo, imágenes externas ni enlaces."""
from __future__ import annotations

import logging
import queue
import threading

import customtkinter as ctk

from ..core.mdview import md_to_html
from .theme import BORDER, FONT, SURFACE, TEXT, font

try:
    from tkinterweb import HtmlFrame
    HAS_WEB = True
except Exception:  # noqa: BLE001
    HAS_WEB = False

log = logging.getLogger(__name__)


class MdView(ctk.CTkFrame):
    def __init__(self, master, app, placeholder: str = ""):
        super().__init__(master, fg_color=SURFACE, corner_radius=12, border_width=1, border_color=BORDER)
        self.app = app
        self.placeholder = placeholder
        self.md = ""
        self._token = 0
        self.q: queue.Queue = queue.Queue()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.web = None
        if HAS_WEB:
            try:
                self.web = HtmlFrame(self, messages_enabled=False)
                self.web.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
            except Exception as exc:  # noqa: BLE001
                log.warning("tkinterweb no disponible: %s", exc)
        self.text = ctk.CTkTextbox(self, wrap="word", fg_color="transparent", font=font(14), text_color=TEXT, border_spacing=14)
        if self.web is None:
            self.text.grid(row=0, column=0, sticky="nsew")
        self.after(120, self._poll)
        self.show(placeholder)

    @property
    def theme(self) -> str:
        return "dark" if self.app.dark else "light"

    def show(self, md: str):
        self.md = md
        if self.web is None:
            from ..pdf.textproc import latex_to_text
            t = self.text._textbox
            t.configure(state="normal")
            t.delete("1.0", "end")
            t.insert("end", latex_to_text(md))
            t.configure(state="disabled")
            return
        self._token += 1
        token, theme, md_ = self._token, self.theme, md or " "

        def page(html, css):
            return f"<html><head><meta charset='utf-8'><style>{css}</style></head><body>{html}</body></html>"
        try:
            self.web.load_html(page(*md_to_html(md_, theme, FONT, formulas=False, safe=True)))
        except Exception as exc:  # noqa: BLE001
            log.warning("Fallo del visor: %s", exc)
            return

        def work():
            try:
                self.q.put((token, page(*md_to_html(md_, theme, FONT, formulas=True, safe=True))))
            except Exception as exc:  # noqa: BLE001
                log.warning("No se pudieron dibujar las fórmulas: %s", exc)
        if "$" in md or "\\[" in md:
            threading.Thread(target=work, daemon=True).start()

    def on_theme(self):
        self.show(self.md)

    def _poll(self):
        try:
            while True:
                token, html = self.q.get_nowait()
                if token == self._token and self.web is not None:
                    self.web.load_html(html)
        except queue.Empty:
            pass
        self.after(150, self._poll)
