"""Ventana de confirmación de acciones del asistente: muestra la acción EXACTA y, por defecto, deniega."""
from __future__ import annotations

import customtkinter as ctk

from .theme import ACCENT, ACCENT_H, BG, BORDER, ERR, MUTED, SURFACE, TEXT, WARN, font

TIMEOUT_MS = 180_000       # sin respuesta en 3 minutos = denegar


def ask(parent, tool: str, summary: str, tainted: bool) -> bool:
    """Bloquea (modal) hasta que el usuario decide. Cerrar la ventana, Esc o el tiempo agotado = DENEGAR."""
    result = {"ok": False}
    win = ctk.CTkToplevel(parent)
    win.title("El asistente quiere hacer algo")
    win.geometry("640x420")
    win.configure(fg_color=BG)
    win.transient(parent.winfo_toplevel())
    win.grab_set()
    win.attributes("-topmost", True)
    win.columnconfigure(0, weight=1)
    win.rowconfigure(2, weight=1)
    ctk.CTkLabel(win, text="El asistente pide permiso para esta acción", font=font(16, "bold"), text_color=TEXT).grid(
        row=0, column=0, sticky="w", padx=20, pady=(18, 4))
    note = "Se ejecutará tal cual se muestra abajo." if tool != "ejecutar_python" else \
        "El código se ejecuta en un entorno aislado: sin red y sin acceso a tus archivos."
    ctk.CTkLabel(win, text=note, font=font(12), text_color=MUTED).grid(row=1, column=0, sticky="w", padx=20)
    box = ctk.CTkTextbox(win, wrap="word", fg_color=SURFACE, text_color=TEXT, border_width=1, border_color=BORDER,
                         font=ctk.CTkFont("Consolas", 12))
    box.grid(row=2, column=0, sticky="nsew", padx=20, pady=10)
    box.insert("1.0", summary)          # tal cual: lo que se aprueba es exactamente lo que se ejecuta
    box.configure(state="disabled")
    if tainted:
        ctk.CTkLabel(win, text="⚠ Esta acción se pidió DESPUÉS de leer contenido externo (un archivo o PDF). Ese contenido "
                     "puede traer instrucciones ocultas. Si tú no lo pediste, deniégala.", wraplength=590, justify="left",
                     font=font(12, "bold"), text_color=ERR).grid(row=3, column=0, sticky="w", padx=20)
    bar = ctk.CTkFrame(win, fg_color="transparent")
    bar.grid(row=4, column=0, sticky="e", padx=20, pady=14)

    def done(ok: bool):
        result["ok"] = ok
        win.grab_release()
        win.destroy()
    deny = ctk.CTkButton(bar, text="Denegar", width=120, height=36, fg_color=ACCENT, hover_color=ACCENT_H, text_color="#ffffff",
                         font=font(13, "bold"), command=lambda: done(False))
    allow = ctk.CTkButton(bar, text="Permitir esta vez", width=150, height=36, fg_color="transparent", border_width=1,
                          border_color=BORDER, text_color=TEXT, hover_color=SURFACE, font=font(13), command=lambda: done(True))
    allow.pack(side="left", padx=(0, 10))
    deny.pack(side="left")
    deny.focus_set()
    win.bind("<Escape>", lambda e: done(False))
    win.protocol("WM_DELETE_WINDOW", lambda: done(False))
    win.after(TIMEOUT_MS, lambda: done(False) if win.winfo_exists() else None)
    parent.wait_window(win)
    return result["ok"]
