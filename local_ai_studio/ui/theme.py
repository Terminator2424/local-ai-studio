"""Tema compartido por todas las páginas: paleta (claro, oscuro), fuente y estado del modo oscuro.

Fondo blanco por defecto, acento rojo, interruptor de modo oscuro. Cada color es una tupla (claro, oscuro) para que
CustomTkinter cambie solo al alternar el modo.
"""
from __future__ import annotations

import json
import logging
import tkinter as tk

import customtkinter as ctk

from ..core.paths import CACHE_DIR

log = logging.getLogger(__name__)

BG = ("#ffffff", "#0f1115")
SURFACE = ("#f6f7f9", "#171a21")
BORDER = ("#e3e5e8", "#2a2f3a")
TEXT = ("#16181d", "#e9ebf0")
MUTED = ("#6b7280", "#9aa3b2")
ACCENT = ("#d62839", "#ef4444")
ACCENT_H = ("#b81f2e", "#dc2626")
OK = ("#15803d", "#4ade80")
WARN = ("#b45309", "#fbbf24")
ERR = ("#b91c1c", "#f87171")
LOG_COLORS = {"light": {"ok": "#15803d", "run": "#d62839", "warn": "#b45309", "err": "#b91c1c", "time": "#9ca3af"},
              "dark": {"ok": "#4ade80", "run": "#ef4444", "warn": "#fbbf24", "err": "#f87171", "time": "#6b7280"}}
UI_STATE = CACHE_DIR / "ui.json"


def pick_font() -> str:
    """Inter si está instalada; si no, Segoe UI."""
    try:
        import tkinter.font as tkfont
        root = tk.Tk()
        root.withdraw()
        fams = set(tkfont.families(root))
        root.destroy()
        return "Inter" if "Inter" in fams else "Segoe UI"
    except Exception:  # noqa: BLE001
        return "Segoe UI"


FONT = pick_font()


def load_dark() -> bool:
    try:
        return bool(json.loads(UI_STATE.read_text(encoding="utf-8")).get("dark", False))
    except Exception:  # noqa: BLE001
        return False


def save_dark(dark: bool):
    try:
        UI_STATE.parent.mkdir(parents=True, exist_ok=True)
        UI_STATE.write_text(json.dumps({"dark": dark}), encoding="utf-8")
    except OSError as exc:
        log.warning("No se pudo guardar el modo oscuro: %s", exc)


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(FONT, size, weight)


def primary_button(parent, text, command, **kw) -> ctk.CTkButton:
    kw.setdefault("height", 36)
    return ctk.CTkButton(parent, text=text, command=command, corner_radius=8, fg_color=ACCENT, hover_color=ACCENT_H,
                         text_color="#ffffff", font=font(13, "bold"), **kw)


def ghost_button(parent, text, command, **kw) -> ctk.CTkButton:
    kw.setdefault("height", 32)
    return ctk.CTkButton(parent, text=text, command=command, corner_radius=8, fg_color="transparent", border_width=1,
                         border_color=BORDER, text_color=TEXT, hover_color=SURFACE, font=font(13), **kw)


def label(parent, text, size=13, weight="normal", muted=False, **kw) -> ctk.CTkLabel:
    return ctk.CTkLabel(parent, text=text, font=font(size, weight), text_color=MUTED if muted else TEXT, **kw)


def option_menu(parent, values, variable=None, command=None, **kw) -> ctk.CTkOptionMenu:
    return ctk.CTkOptionMenu(parent, values=values, variable=variable, command=command, height=32, fg_color=SURFACE,
                             button_color=ACCENT, button_hover_color=ACCENT_H, text_color=TEXT, dropdown_fg_color=SURFACE,
                             dropdown_text_color=TEXT, dropdown_hover_color=BORDER, font=font(13), **kw)


def slider(parent, **kw) -> ctk.CTkSlider:
    return ctk.CTkSlider(parent, progress_color=ACCENT, button_color=ACCENT, button_hover_color=ACCENT_H,
                         fg_color=BORDER, **kw)
